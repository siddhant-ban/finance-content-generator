"""Unit tests for Scout editorial assignments, deduplication, cost reporting, and export gates."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

import pytest

from app.contracts.common import ArtifactRef
from app.contracts.evidence import SourceRecord
from app.contracts.scout import (
    AttributedStatement,
    SCOUT_SCHEMA,
    ScoutConfiguration,
    ScoutFingerprintSnapshot,
    ScoutLimits,
    ScoutMember,
    ScoutRunInput,
    ScoutTopicPolicy,
    StoryCandidate,
    candidate_fingerprint,
    source_ref,
)
from app.contracts.workflow import StoryState
from app.scout.editorial import (
    EditorialDecisionError,
    ExportGateError,
    RunCostAttempt,
    candidate_editorial_status,
    export_adjudicated_failures,
    generate_pilot_report,
    record_decision,
    record_pilot_run,
)
from app.scout.ledger import SpendLedger
from app.scout.workflow import deliver_candidate, record_story_revision, scout_state
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database

T0 = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _make_member(
    *,
    source_id: UUID | None = None,
    revision: int = 1,
    url: str = "https://www.bls.gov/news.release/cpi_01.htm",
    publisher: str = "U.S. Bureau of Labor Statistics",
    headline: str = "Consumer Price Index - October 2026",
    rights: str = "metadata_only",
    excerpt: str | None = None,
) -> ScoutMember:
    src = SourceRecord(
        source_id=source_id or uuid4(),
        revision=revision,
        original_url=url,
        canonical_url=url,
        publisher=publisher,
        source_class="primary",
        published_at=T0 - timedelta(hours=1),
        publication_time_reason=None,
        retrieved_at=T0,
        locator="Opening paragraph",
        excerpt=excerpt,
        rights=rights,
        usage_notes="Official release metadata.",
    )
    return ScoutMember(headline=headline, source=src, ref=source_ref(src))


def _pin_run_request(store: ArtifactStore, run_id: UUID) -> None:
    req = ScoutRunInput(
        schema_version="1.0",
        run_id=run_id,
        invocation_id=uuid4(),
        window_start=T0 - timedelta(hours=2),
        window_end=T0 + timedelta(hours=1),
        topic_policy=ScoutTopicPolicy(version="v1", topics=("prices", "jobs", "borrowing")),
        fingerprints=ScoutFingerprintSnapshot(version="v1", known=()),
        configuration=ScoutConfiguration(
            version="v1",
            feed_snapshot="a" * 64,
            cluster_version="scout.cluster.v1",
            prompt_version="scout_enrichment.v1",
            prompt_hash="b" * 64,
            provider="openrouter",
            model="inclusionai/ling-3.2-flash:free",
            temperature=0.0,
        ),
        release_id=uuid4(),
        limits=ScoutLimits(max_items=10, max_tokens=1500, timeout_seconds=30.0),
    )
    ref = store.put(req.canonical_bytes(), "application/json", "1.0")
    with store.conn:
        store.conn.execute(
            "INSERT INTO scout_run_requests(run_id, invocation_id, input_sha256) VALUES (?, ?, ?)",
            (str(run_id), str(req.invocation_id), ref.sha256),
        )


def _create_candidate(
    store: ArtifactStore,
    *,
    run_id: UUID,
    cluster_id: UUID | None = None,
    revision: int = 1,
    members: tuple[ScoutMember, ...] | None = None,
    cluster_key: str = "event:cpi:2026-10",
) -> tuple[StoryCandidate, ArtifactRef]:
    from uuid import uuid5

    cid = cluster_id or uuid4()
    sid = uuid5(cid, "story")
    mems = members or (_make_member(),)
    candidate = StoryCandidate(
        schema_version="1.0",
        schema_id=SCOUT_SCHEMA,
        kind="editorial",
        story_id=sid,
        cluster_id=cid,
        revision=revision,
        fingerprint=candidate_fingerprint(mems),
        representative=mems[0].ref,
        members=mems,
        summary=(AttributedStatement(text="CPI rose 0.2 percent.", sources=(mems[0].ref,)),),
        topics=("prices",),
        entities=("Consumer Price Index",),
        warnings=("seasonally_adjusted",),
        uncertainty=(),
    )
    ref = store.put(candidate.canonical_bytes(), "application/json", "1.0")
    workflow = scout_state(store)
    deliver_candidate(workflow, candidate, ref, run_id, "0" * 31 + "1")
    record_story_revision(
        store,
        candidate=candidate,
        ref=ref,
        run_id=run_id,
        cluster_key=cluster_key,
        cluster_version="scout.cluster.v1",
        reason_code="candidate_created" if revision == 1 else "source_correction",
        created_at=T0,
    )
    return candidate, ref


@pytest.fixture
def scout_db(tmp_path, monkeypatch):
    # Prevent Windows os.open(dir) PermissionError in unit tests exercising ArtifactStore.
    import os
    if os.name == "nt":
        import app.storage.artifacts as artifacts_mod
        real_open, real_fsync, real_close = os.open, os.fsync, os.close
        sentinel_fds: set[int] = set()
        next_fd = 950_000

        class _WinSafeOs:
            O_RDONLY = os.O_RDONLY

            def __getattr__(self, name):
                return getattr(os, name)

            def open(self, path, flags, *args, **kwargs):
                nonlocal next_fd
                if os.path.isdir(path):
                    next_fd += 1
                    sentinel_fds.add(next_fd)
                    return next_fd
                return real_open(path, flags, *args, **kwargs)

            def fsync(self, fd):
                if fd not in sentinel_fds:
                    real_fsync(fd)

            def close(self, fd):
                if fd in sentinel_fds:
                    sentinel_fds.discard(fd)
                    return
                real_close(fd)

        monkeypatch.setattr(artifacts_mod, "os", _WinSafeOs())

    conn = open_database(tmp_path / "scout.sqlite3")
    migrate(conn)
    store = ArtifactStore(tmp_path / "artifacts", conn)
    yield conn, store, tmp_path
    conn.close()


def test_migration_013_creates_append_only_tables_and_triggers(scout_db):
    conn, store, _ = scout_db
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    assert {"scout_editorial_decisions", "scout_pilot_runs"} <= tables
    assert "013_scout_editorial_assignments.sql" in applied

    run_id = uuid4()
    _pin_run_request(store, run_id)
    cand, ref = _create_candidate(store, run_id=run_id)
    dec = record_decision(
        conn,
        story_id=cand.story_id,
        revision=cand.revision,
        run_id=run_id,
        candidate_sha256=ref.sha256,
        decision="assigned",
        reason_code="newsworthy_macro",
        notes="Assign for coverage.",
        actor_id="editor-1",
        decided_at=T0 + timedelta(minutes=5),
    )

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(
            "UPDATE scout_editorial_decisions SET decision = 'passed' WHERE decision_id = ?",
            (str(dec.decision_id),),
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(
            "DELETE FROM scout_editorial_decisions WHERE decision_id = ?",
            (str(dec.decision_id),),
        )
    conn.rollback()

    record_pilot_run(
        conn,
        run_id=run_id,
        recorded_at=T0,
        outcome="complete",
        feed_ids=("bls-cpi",),
    )
    with pytest.raises(sqlite3.DatabaseError, match="immutable|append-only"):
        conn.execute(
            "UPDATE scout_pilot_runs SET outcome = 'failed' WHERE run_id = ?",
            (str(run_id),),
        )
    with pytest.raises(sqlite3.DatabaseError, match="immutable|append-only"):
        conn.execute(
            "DELETE FROM scout_pilot_runs WHERE run_id = ?",
            (str(run_id),),
        )


def test_decision_validation_binding_and_workflow_isolation(scout_db):
    conn, store, _ = scout_db
    run_id = uuid4()
    _pin_run_request(store, run_id)
    cand, ref = _create_candidate(store, run_id=run_id)

    # Pending before any decision exists.
    assert candidate_editorial_status(conn, story_id=cand.story_id, revision=cand.revision) == "pending"

    # Reject mismatched candidate_sha256.
    with pytest.raises(EditorialDecisionError, match="candidate_sha256"):
        record_decision(
            conn,
            story_id=cand.story_id,
            revision=cand.revision,
            run_id=run_id,
            candidate_sha256="f" * 64,
            decision="assigned",
            reason_code="macro",
            notes="",
            actor_id="editor-1",
        )

    # Reject unknown story/revision.
    with pytest.raises(EditorialDecisionError, match="unknown_story_revision"):
        record_decision(
            conn,
            story_id=uuid4(),
            revision=1,
            run_id=run_id,
            candidate_sha256=ref.sha256,
            decision="passed",
            reason_code="off_topic",
            notes="",
            actor_id="editor-1",
        )

    # Reject unknown run_id, invalid 'pending' stored decision, and blank metadata.
    with pytest.raises(EditorialDecisionError, match="unknown_run_id"):
        record_decision(
            conn,
            story_id=cand.story_id,
            revision=cand.revision,
            run_id=uuid4(),
            candidate_sha256=ref.sha256,
            decision="passed",
            reason_code="off_topic",
            notes="",
            actor_id="editor-1",
        )
    with pytest.raises(EditorialDecisionError, match="invalid_editorial_decision"):
        record_decision(
            conn,
            story_id=cand.story_id,
            revision=cand.revision,
            run_id=run_id,
            candidate_sha256=ref.sha256,
            decision="pending",  # type: ignore[arg-type]
            reason_code="triage",
            notes="",
            actor_id="editor-1",
        )
    with pytest.raises(EditorialDecisionError, match="reason_code"):
        record_decision(
            conn,
            story_id=cand.story_id,
            revision=cand.revision,
            run_id=run_id,
            candidate_sha256=ref.sha256,
            decision="passed",
            reason_code="   ",
            notes="",
            actor_id="editor-1",
        )

    # Valid assignment leaves workflow state in CLUSTERED and does not touch approvals/releases.
    dec = record_decision(
        conn,
        story_id=cand.story_id,
        revision=cand.revision,
        run_id=run_id,
        candidate_sha256=ref.sha256,
        decision="assigned",
        reason_code="cpi_release",
        notes="Strong primary signal",
        actor_id="editor-alice",
        decided_at=T0 + timedelta(minutes=1),
    )
    assert dec.story_id == cand.story_id
    assert dec.revision == 1
    assert dec.run_id == run_id
    assert dec.candidate_sha256 == ref.sha256
    assert dec.decision == "assigned"
    assert candidate_editorial_status(conn, story_id=cand.story_id, revision=cand.revision) == "assigned"

    snap = scout_state(store).get(cand.story_id)
    assert snap.state == StoryState.CLUSTERED
    assert snap.approval_id is None
    assert conn.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM release_approvals").fetchone()[0] == 0


def test_distinct_story_first_assignment_dedup_and_multi_feed_yield(scout_db):
    conn, store, _ = scout_db
    r1, r2, r3, r_empty, r_fail = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    for rid in (r1, r2, r3):
        _pin_run_request(store, rid)

    # Story 1 rev 1 covered by both feed-a and feed-b in R1.
    cid1 = uuid4()
    src_a = _make_member(url="https://example.com/cpi-a")
    src_b = _make_member(url="https://example.com/cpi-b")
    s1_r1, ref1_r1 = _create_candidate(store, run_id=r1, cluster_id=cid1, revision=1, members=(src_a, src_b))

    # Story 2 rev 1 covered only by feed-a in R1 (will be passed).
    cid2 = uuid4()
    s2_r1, ref2_r1 = _create_candidate(
        store,
        run_id=r1,
        cluster_id=cid2,
        revision=1,
        members=(_make_member(url="https://example.com/jobs-a"),),
        cluster_key="event:jobs:2026-10",
    )

    # Story 3 rev 1 covered by feed-b in R2 (will remain pending).
    cid3 = uuid4()
    s3_r1, ref3_r1 = _create_candidate(
        store,
        run_id=r2,
        cluster_id=cid3,
        revision=1,
        members=(_make_member(url="https://example.com/fed-b"),),
        cluster_key="event:fed:2026-10",
    )

    # Story 1 gets a correction (rev 2) in R3 from feed-b.
    src_a_v2 = _make_member(source_id=src_a.source.source_id, revision=2, url="https://example.com/cpi-a")
    s1_r2, ref1_r2 = _create_candidate(store, run_id=r3, cluster_id=cid1, revision=2, members=(src_a_v2, src_b))

    record_pilot_run(
        conn,
        run_id=r1,
        recorded_at=T0,
        outcome="complete",
        feed_ids=("feed-a", "feed-b", "feed-quiet"),
        candidate_feeds={
            ref1_r1.sha256: ("feed-a", "feed-b"),
            ref2_r1.sha256: ("feed-a",),
        },
        store=store,
    )
    record_pilot_run(
        conn,
        run_id=r2,
        recorded_at=T0 + timedelta(hours=1),
        outcome="complete",
        feed_ids=("feed-b", "feed-quiet"),
        candidate_feeds={ref3_r1.sha256: ("feed-b",)},
        store=store,
    )
    record_pilot_run(
        conn,
        run_id=r3,
        recorded_at=T0 + timedelta(hours=2),
        outcome="complete",
        feed_ids=("feed-b",),
        candidate_feeds={ref1_r2.sha256: ("feed-b",)},
        store=store,
    )
    record_pilot_run(
        conn,
        run_id=r_empty,
        recorded_at=T0 + timedelta(hours=3),
        outcome="empty",
        feed_ids=("feed-a", "feed-quiet"),
    )
    record_pilot_run(
        conn,
        run_id=r_fail,
        recorded_at=T0 + timedelta(hours=4),
        outcome="failed",
        feed_ids=("feed-b",),
    )

    # S1 rev 1: passed first, then assigned (first assigned at T0+10m in R1), then assigned again on rev 1 and rev 2!
    record_decision(
        conn, story_id=s1_r1.story_id, revision=1, run_id=r1, candidate_sha256=ref1_r1.sha256,
        decision="passed", reason_code="wait_for_details", notes="", actor_id="ed-1",
        decided_at=T0 + timedelta(minutes=5),
    )
    record_decision(
        conn, story_id=s1_r1.story_id, revision=1, run_id=r1, candidate_sha256=ref1_r1.sha256,
        decision="assigned", reason_code="confirmed", notes="", actor_id="ed-2",
        decided_at=T0 + timedelta(minutes=10),
    )
    record_decision(
        conn, story_id=s1_r1.story_id, revision=1, run_id=r1, candidate_sha256=ref1_r1.sha256,
        decision="assigned", reason_code="duplicate_click", notes="", actor_id="ed-2",
        decided_at=T0 + timedelta(minutes=11),
    )
    record_decision(
        conn, story_id=s1_r2.story_id, revision=2, run_id=r3, candidate_sha256=ref1_r2.sha256,
        decision="assigned", reason_code="correction_reviewed", notes="", actor_id="ed-1",
        decided_at=T0 + timedelta(hours=2, minutes=5),
    )
    # S2 rev 1: passed.
    record_decision(
        conn, story_id=s2_r1.story_id, revision=1, run_id=r1, candidate_sha256=ref2_r1.sha256,
        decision="passed", reason_code="minor_revision", notes="", actor_id="ed-1",
        decided_at=T0 + timedelta(minutes=15),
    )
    # S3 rev 1: left pending!

    report = generate_pilot_report(conn)
    totals = report.channel_totals
    assert totals.total_runs == 5
    assert totals.empty_runs == 1
    assert totals.failed_runs == 1
    assert totals.assignment_producing_runs == 1  # Only R1 produced S1's first assignment.
    assert totals.total_candidates == 3
    assert totals.distinct_assigned == 1
    assert totals.distinct_passed == 1
    assert totals.reviewed_stories == 2
    assert totals.pending_stories == 1
    assert totals.review_coverage == pytest.approx(2 / 3)
    assert totals.assigned_per_reviewed == pytest.approx(0.5)
    assert totals.total_decision_events == 5

    by_feed = {row.feed_id: row for row in report.by_feed}
    assert by_feed["feed-a"].unique_candidates == 2
    assert by_feed["feed-a"].unique_assignments == 1
    assert by_feed["feed-a"].runs_participated == 2
    assert by_feed["feed-b"].unique_candidates == 2  # S1 + S3 (S1 counted once across rev 1 & rev 2)
    assert by_feed["feed-b"].unique_assignments == 1
    assert by_feed["feed-b"].runs_participated == 4
    assert by_feed["feed-quiet"].unique_candidates == 0
    assert by_feed["feed-quiet"].unique_assignments == 0
    assert by_feed["feed-quiet"].runs_participated == 3
    # Sum of per-feed candidates (4) > channel total (3) without inflating channel totals!
    assert sum(row.unique_candidates for row in report.by_feed) > totals.total_candidates


def test_cost_classification_ledger_integration_and_zero_assignment_undefined(scout_db):
    conn, store, tmp_path = scout_db
    r1, r2, r_empty, r_fail = uuid4(), uuid4(), uuid4(), uuid4()
    _pin_run_request(store, r1)
    cand, ref = _create_candidate(store, run_id=r1)

    ledger_path = tmp_path / "ledger.sqlite3"
    with closing(open_database(ledger_path)) as lconn:
        migrate(lconn)
    ledger = SpendLedger(ledger_path)
    ledger.ensure_experiment("exp-pilot", hard_spend_cap_usd=Decimal("1.00"), config_hash="c" * 64)

    res_reconciled = ledger.reserve(
        experiment_id="exp-pilot",
        profile_name="paid",
        model_id="openai/gpt-4o-mini",
        example_id="ex-1",
        attempt_key="ex-1:0",
        worst_case_usd=Decimal("0.01"),
    )
    ledger.reconcile(
        res_reconciled.reservation_id,
        actual_usd=Decimal("0.0025"),
        input_tokens=500,
        output_tokens=100,
        resolved_model="openai/gpt-4o-mini",
    )
    res_ambig = ledger.reserve(
        experiment_id="exp-pilot",
        profile_name="paid",
        model_id="openai/gpt-4o-mini",
        example_id="ex-fail",
        attempt_key="ex-fail:0",
        worst_case_usd=Decimal("0.0050"),
    )
    ledger.mark_ambiguous(res_ambig.reservation_id, failure_disposition="timeout")
    res_released = ledger.reserve(
        experiment_id="exp-pilot",
        profile_name="paid",
        model_id="openai/gpt-4o-mini",
        example_id="ex-pre",
        attempt_key="ex-pre:0",
        worst_case_usd=Decimal("0.0050"),
    )
    ledger.release(res_released.reservation_id, failure_disposition="rate_exceeds_profile")

    record_pilot_run(
        conn,
        run_id=r1,
        recorded_at=T0,
        outcome="complete",
        feed_ids=("bls-cpi",),
        candidate_feeds={ref.sha256: ("bls-cpi",)},
        reservation_ids=(res_reconciled.reservation_id, res_released.reservation_id),
        cost_records=(
            RunCostAttempt(
                attempt_id="est-1",
                cost_kind="estimated",
                cost_usd=Decimal("0.0015"),
                input_tokens=300,
                output_tokens=50,
                model_id="openai/gpt-4o-mini",
                reason="rate_table_fallback",
            ),
        ),
        store=store,
    )
    record_pilot_run(
        conn,
        run_id=r_empty,
        recorded_at=T0 + timedelta(minutes=10),
        outcome="empty",
        feed_ids=("bls-cpi",),
    )
    record_pilot_run(
        conn,
        run_id=r_fail,
        recorded_at=T0 + timedelta(minutes=20),
        outcome="failed",
        feed_ids=("bls-cpi",),
        reservation_ids=(res_ambig.reservation_id,),
        cost_records=(
            RunCostAttempt(
                attempt_id="miss-1",
                cost_kind="missing",
                cost_usd=None,
                reason="provider_usage_omitted",
            ),
        ),
    )

    with closing(open_database(ledger_path)) as lconn:
        # Before any assignment: cost_per_distinct_assignment_usd MUST be None!
        zero_report = generate_pilot_report(conn, ledger_conn=lconn)
        assert zero_report.channel_totals.total_runs == 3
        assert zero_report.channel_totals.distinct_assigned == 0
        assert zero_report.cost.measured_cost_usd == Decimal("0.0025")
        assert zero_report.cost.estimated_cost_usd == Decimal("0.0015")
        assert zero_report.cost.total_model_cost_usd == Decimal("0.004")
        assert zero_report.cost.measured_cost_attempts == 1
        assert zero_report.cost.estimated_cost_attempts == 1
        assert zero_report.cost.missing_cost_attempts == 2  # res_ambig + miss-1
        assert zero_report.cost.released_attempts == 1
        assert zero_report.cost.ambiguous_or_pending_reserved_usd == Decimal("0.005")
        assert zero_report.cost.cost_per_distinct_assignment_usd is None
        assert len(zero_report.cost.excluded_costs) >= 1
        assert "labor" in zero_report.cost.excluded_costs_note.lower()

        # Assign the candidate -> cost_per_distinct_assignment_usd becomes defined!
        record_decision(
            conn,
            story_id=cand.story_id,
            revision=cand.revision,
            run_id=r1,
            candidate_sha256=ref.sha256,
            decision="assigned",
            reason_code="cpi_release",
            notes="",
            actor_id="ed-1",
            decided_at=T0 + timedelta(minutes=30),
        )
        assigned_report = generate_pilot_report(conn, ledger_conn=lconn)
        assert assigned_report.channel_totals.distinct_assigned == 1
        assert assigned_report.cost.cost_per_distinct_assignment_usd == Decimal("0.004")


def test_export_adjudicated_failures_enforces_rights_privacy_and_no_pii(scout_db):
    conn, store, tmp_path = scout_db
    run_id = uuid4()
    _pin_run_request(store, run_id)

    cand_ok, ref_ok = _create_candidate(
        store,
        run_id=run_id,
        members=(_make_member(rights="excerpt_permitted", excerpt="Short permitted official excerpt."),),
    )
    record_decision(
        conn,
        story_id=cand_ok.story_id,
        revision=cand_ok.revision,
        run_id=run_id,
        candidate_sha256=ref_ok.sha256,
        decision="passed",
        reason_code="irrelevant_minor_revision",
        notes="Routine footnote update only.",
        actor_id="private-reviewer-alice@example.com",
        decided_at=T0 + timedelta(minutes=5),
    )

    # Gate 1: rights_cleared must be True.
    with pytest.raises(ExportGateError, match="rights"):
        export_adjudicated_failures(conn, store, rights_cleared=False, privacy_checked=True)

    # Gate 2: privacy_checked must be True.
    with pytest.raises(ExportGateError, match="privacy"):
        export_adjudicated_failures(conn, store, rights_cleared=True, privacy_checked=False)

    # Gate 3: valid export succeeds and never leaks actor_id PII.
    out_file = tmp_path / "adjudicated_failures.json"
    exported = export_adjudicated_failures(
        conn,
        store,
        output_path=out_file,
        rights_cleared=True,
        privacy_checked=True,
        dataset_version="1.1.0-draft",
    )
    assert exported.record_count == 1
    assert exported.records[0].decision == "passed"
    assert exported.records[0].source_pins[0].excerpt == "Short permitted official excerpt."
    raw_json = out_file.read_text(encoding="utf-8")
    assert "private-reviewer-alice" not in raw_json
    assert "@example.com" not in raw_json

    # Gate 4: refuses to write into sealed evals/ directories.
    with pytest.raises(ExportGateError, match="frozen|dataset"):
        export_adjudicated_failures(
            conn,
            store,
            output_path=Path("evals/news_scout/1.0.0/dev.json"),
            rights_cleared=True,
            privacy_checked=True,
        )

    # Gate 5: reject secret or PII embedded in editorial notes.
    cand_pii, ref_pii = _create_candidate(
        store,
        run_id=run_id,
        cluster_id=uuid4(),
        members=(_make_member(url="https://example.com/other"),),
        cluster_key="event:jobs:2026-10",
    )
    record_decision(
        conn,
        story_id=cand_pii.story_id,
        revision=cand_pii.revision,
        run_id=run_id,
        candidate_sha256=ref_pii.sha256,
        decision="passed",
        reason_code="bad_item",
        notes="Contact reviewer@corp.test or use key sk-secret1234567890",
        actor_id="ed-2",
        decided_at=T0 + timedelta(minutes=6),
    )
    with pytest.raises(ExportGateError, match="pii|secret"):
        export_adjudicated_failures(conn, store, rights_cleared=True, privacy_checked=True)


def test_window_filtering_empty_ratios_experiment_lookup_and_metadata_only_excerpt_gate(scout_db):
    conn, store, tmp_path = scout_db

    # Empty pilot report has undefined ratios and None cost per assignment.
    empty_report = generate_pilot_report(conn)
    assert empty_report.channel_totals.total_runs == 0
    assert empty_report.channel_totals.review_coverage is None
    assert empty_report.channel_totals.assigned_per_reviewed is None
    assert empty_report.cost.cost_per_distinct_assignment_usd is None

    r1, r2 = uuid4(), uuid4()
    _pin_run_request(store, r1)
    cand, ref = _create_candidate(
        store,
        run_id=r1,
        members=(_make_member(rights="excerpt_permitted", excerpt="Long copyrighted body " * 30),),
    )

    ledger_path = tmp_path / "ledger.sqlite3"
    with closing(open_database(ledger_path)) as lconn:
        migrate(lconn)
    ledger = SpendLedger(ledger_path)
    ledger.ensure_experiment("exp-win", hard_spend_cap_usd=Decimal("1.00"), config_hash="e" * 64)
    res_est = ledger.reserve(
        experiment_id="exp-win",
        profile_name="paid",
        model_id="openai/gpt-4o-mini",
        example_id="ex-win-1",
        attempt_key="ex-win-1:0",
        worst_case_usd=Decimal("0.01"),
    )
    ledger.reconcile(
        res_est.reservation_id,
        actual_usd=Decimal("0.0012"),
        input_tokens=100,
        output_tokens=50,
    )

    record_pilot_run(
        conn,
        run_id=r1,
        recorded_at=T0,
        outcome="complete",
        feed_ids=("bls-cpi",),
        candidate_feeds={ref.sha256: ("bls-cpi",)},
        experiment_id="exp-win",
        example_id="ex-win-1",
        reservation_cost_kinds={res_est.reservation_id: "estimated"},
        store=store,
    )
    record_pilot_run(
        conn,
        run_id=r2,
        recorded_at=T0 + timedelta(days=2),
        outcome="empty",
        feed_ids=("bls-cpi",),
    )

    with closing(open_database(ledger_path)) as lconn:
        windowed = generate_pilot_report(
            conn,
            ledger_conn=lconn,
            window_start=T0,
            window_end=T0 + timedelta(days=1),
        )
        assert windowed.selected_run_ids == (r1,)
        assert windowed.cost.estimated_cost_usd == Decimal("0.0012")
        assert windowed.cost.estimated_cost_attempts == 1
        assert windowed.cost.measured_cost_attempts == 0

    record_decision(
        conn,
        story_id=cand.story_id,
        revision=cand.revision,
        run_id=r1,
        candidate_sha256=ref.sha256,
        decision="passed",
        reason_code="copyright_check",
        notes="",
        actor_id="ed-1",
        decided_at=T0 + timedelta(minutes=1),
    )
    with pytest.raises(ExportGateError, match="copyrighted_body_exceeds_excerpt_limit"):
        export_adjudicated_failures(conn, store, rights_cleared=True, privacy_checked=True)
