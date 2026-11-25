"""Integration tests for durable delivery recovery, overlapping runs, offline audit replay, and reeval."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import itertools
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

import pytest

from app.contracts.evidence import SourceRecord
from app.contracts.scout import ScoutEnrichment, ScoutFeedItem, ScoutRunInput, ScoutRunOutput, ScoutTokenUsage, StoryCandidate
from app.contracts.workflow import StoryState
from app.observability.tracing import Telemetry
from app.scout.cli import main
from app.scout.cluster import CLUSTER_VERSION
from app.scout.delivery import get_deliveries, recover_pending_deliveries
from app.scout.ledger import SpendLedger
from app.scout.replay import reeval_scout, replay_scout
from app.scout.run import ModelResult, run_scout
from app.scout.workflow import scout_state, story_revisions
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database


@pytest.fixture(autouse=True)
def _windows_artifact_dir_sync_compat(monkeypatch):
    """Windows cannot os.open directories with O_RDONLY; keep file fsync real."""
    if os.name != "nt":
        return
    import app.storage.artifacts as artifacts_mod

    original_open = os.open
    original_close = os.close
    original_fsync = os.fsync
    dir_fds: set[int] = set()
    seq = itertools.count(-2000, -1)

    def win_open(path, flags, *args, **kwargs):
        if flags == os.O_RDONLY and os.path.isdir(path):
            fd = next(seq)
            dir_fds.add(fd)
            return fd
        return original_open(path, flags, *args, **kwargs)

    def win_close(fd):
        if fd in dir_fds:
            dir_fds.remove(fd)
        else:
            original_close(fd)

    def win_fsync(fd):
        if fd not in dir_fds:
            original_fsync(fd)

    monkeypatch.setattr(artifacts_mod.os, "open", win_open)
    monkeypatch.setattr(artifacts_mod.os, "close", win_close)
    monkeypatch.setattr(artifacts_mod.os, "fsync", win_fsync)


@pytest.fixture
def env(tmp_path):
    telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)
        yield tmp_path, conn, ArtifactStore(tmp_path / "artifacts", conn), telemetry
    telemetry.shutdown(timeout_seconds=0)


def _request(
    *,
    model: str = "recorded-v1",
    prompt_version: str = "v1",
    prompt_hash: str = "1" * 64,
    max_tokens: int = 2000,
) -> ScoutRunInput:
    return ScoutRunInput.model_validate_json(json.dumps({
        "schema_version": "1.0",
        "run_id": str(uuid4()),
        "invocation_id": str(uuid4()),
        "window_start": "2025-09-01T00:00:00Z",
        "window_end": "2025-12-01T00:00:00Z",
        "topic_policy": {"version": "v1", "topics": ["prices", "jobs", "borrowing"]},
        "fingerprints": {"version": "v1", "known": []},
        "configuration": {
            "version": "v1",
            "feed_snapshot": "0" * 64,
            "cluster_version": CLUSTER_VERSION,
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "provider": "fake",
            "model": model,
            "temperature": 0.0,
        },
        "release_id": str(uuid4()),
        "limits": {"max_items": 20, "max_tokens": max_tokens, "timeout_seconds": 15},
    }))


def _feed_item(
    *,
    headline: str,
    canonical_url: str,
    publisher: str = "U.S. Bureau of Labor Statistics",
    source_class: str = "primary",
    published_at: datetime | None = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc),
    excerpt: str | None = "The all-items CPI-U rose 0.3 percent in September 2025.",
    rights: str | None = None,
    locator: str = "Opening paragraph",
    revision: int = 1,
    source_id=None,
) -> ScoutFeedItem:
    resolved_rights = rights or ("excerpt_permitted" if excerpt is not None else "metadata_only")
    retrieved = (published_at + timedelta(minutes=10)) if published_at else datetime(2025, 10, 15, 14, 0, tzinfo=timezone.utc)
    return ScoutFeedItem(
        headline=headline,
        source=SourceRecord(
            source_id=source_id or uuid4(),
            revision=revision,
            original_url=canonical_url,
            canonical_url=canonical_url,
            publisher=publisher,
            source_class=source_class,
            published_at=published_at,
            publication_time_reason=None if published_at else "Missing publication date",
            retrieved_at=retrieved,
            locator=locator,
            excerpt=excerpt if resolved_rights == "excerpt_permitted" else None,
            rights=resolved_rights,
            usage_notes="Test fixture source.",
        ),
    )


def _enrich(_request, members, **_kwargs):
    summary = [
        {
            "text": f"Reported by {member.source.publisher}: {member.headline}.",
            "sources": [member.ref.model_dump(mode="json")],
        }
        for member in members
    ]
    enrichment = ScoutEnrichment.model_validate_json(json.dumps({
        "schema_version": "1.0",
        "summary": summary,
        "topics": ["prices"],
        "entities": ["Consumer Price Index"],
        "warnings": ["seasonally_adjusted"],
    }))
    return ModelResult(enrichment.canonical_bytes(), ScoutTokenUsage(input_tokens=10, output_tokens=8))


class SimulatedCrash(BaseException):
    pass


def test_crash_after_intent_and_during_delivery_recovers_cleanly(env, monkeypatch):
    _, conn, store, telemetry = env
    item = _feed_item(
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
    )
    req = _request()

    import app.scout.delivery as delivery_mod
    original_deliver = delivery_mod.deliver_candidate

    def crash_mid_deliver(workflow, candidate, ref, run_id, trace_id):
        # Simulate crash after story is created in DISCOVERED before CLUSTERED transition.
        workflow.create(candidate.story_id, run_id, ref, "news_scout", ref.sha256)
        raise SimulatedCrash("power loss mid-delivery")

    monkeypatch.setattr(delivery_mod, "deliver_candidate", crash_mid_deliver)

    with pytest.raises(SimulatedCrash):
        run_scout(req, store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item.canonical_bytes()}, enrich=_enrich)

    pending = get_deliveries(conn, status="pending")
    assert len(pending) == 1
    assert pending[0].run_id == req.run_id
    assert pending[0].revision == 1
    assert scout_state(store).get(pending[0].story_id).state == StoryState.DISCOVERED

    monkeypatch.setattr(delivery_mod, "deliver_candidate", original_deliver)
    recovered = recover_pending_deliveries(store)
    assert len(recovered) == 1
    assert recovered[0].status == "delivered"
    assert get_deliveries(conn, status="pending") == ()

    state = scout_state(store)
    snap = state.get(pending[0].story_id)
    assert snap.state == StoryState.CLUSTERED
    assert [e.to_state for e in state.events(pending[0].story_id)] == [StoryState.DISCOVERED, StoryState.CLUSTERED]
    assert len(story_revisions(store, pending[0].story_id)) == 1
    # Second recovery call is a clean no-op.
    assert recover_pending_deliveries(store) == ()


def test_lost_response_retry_with_same_run_id_completes_once(env, monkeypatch):
    _, conn, store, telemetry = env
    item = _feed_item(
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
    )
    req = _request()

    import app.scout.delivery as delivery_mod
    original_deliver = delivery_mod.deliver_candidate

    def crash_before_deliver(workflow, candidate, ref, run_id, trace_id):
        raise SimulatedCrash("lost response before dispatch")

    monkeypatch.setattr(delivery_mod, "deliver_candidate", crash_before_deliver)
    with pytest.raises(SimulatedCrash):
        run_scout(req, store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item.canonical_bytes()}, enrich=_enrich)

    monkeypatch.setattr(delivery_mod, "deliver_candidate", original_deliver)
    enrich_calls = 0

    def counting_enrich(request, members, **kwargs):
        nonlocal enrich_calls
        enrich_calls += 1
        return _enrich(request, members, **kwargs)

    # Retry with the EXACT same run_id and invocation_id.
    out1 = run_scout(req, store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item.canonical_bytes()}, enrich=counting_enrich)
    assert out1.outcome == "complete"
    assert [(i.status, i.reason) for i in out1.items] == [("new", "candidate_created")]
    assert enrich_calls == 0  # Reused persisted validated response from pre-dispatch snapshot!

    # Second lost-response retry after completion returns identical output without re-running enrich.
    out2 = run_scout(req, store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item.canonical_bytes()}, enrich=counting_enrich)
    assert out2 == out1
    assert enrich_calls == 0

    deliveries = get_deliveries(conn)
    assert len(deliveries) == 1
    assert deliveries[0].status == "delivered"
    events = scout_state(store).events(deliveries[0].story_id)
    assert [e.to_state for e in events] == [StoryState.DISCOVERED, StoryState.CLUSTERED]


def test_overlapping_threaded_invocations_single_handoff_no_lost_candidates(tmp_path):
    db_path = tmp_path / "scout.sqlite3"
    artifacts_dir = tmp_path / "artifacts"
    with closing(open_database(db_path)) as conn:
        migrate(conn)

    shared_item = _feed_item(
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
    )
    extra_item = _feed_item(
        headline="Employment Situation - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/empsit_10032025.htm",
        published_at=datetime(2025, 10, 3, 12, 30, tzinfo=timezone.utc),
        excerpt="Total nonfarm payroll employment rose by 150,000 in September.",
    )
    barrier = threading.Barrier(2, timeout=5.0)

    def worker(batch: dict[str, bytes]) -> ScoutRunOutput:
        telemetry = Telemetry(tmp_path / f"telemetry-{uuid4()}.sqlite3")
        with closing(open_database(db_path)) as conn:
            store = ArtifactStore(artifacts_dir, conn)
            def sync_enrich(request, members, **kwargs):
                if any("cpi_10152025" in m.source.canonical_url for m in members):
                    barrier.wait()
                return _enrich(request, members, **kwargs)
            try:
                return run_scout(
                    _request(),
                    store=store,
                    telemetry=telemetry,
                    fetch=lambda *_, **__: batch,
                    enrich=sync_enrich,
                )
            finally:
                telemetry.shutdown(timeout_seconds=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(worker, {"shared": shared_item.canonical_bytes()})
        f2 = pool.submit(worker, {"shared": shared_item.canonical_bytes(), "extra": extra_item.canonical_bytes()})
        out1 = f1.result()
        out2 = f2.result()

    assert out1.outcome == "complete"
    assert out2.outcome == "complete"
    with closing(open_database(db_path)) as conn:
        store = ArtifactStore(artifacts_dir, conn)
        deliveries = get_deliveries(conn)
        assert len(deliveries) == 2
        assert all(d.status == "delivered" for d in deliveries)
        state = scout_state(store)
        for d in deliveries:
            events = state.events(d.story_id)
            assert [e.to_state for e in events] == [StoryState.DISCOVERED, StoryState.CLUSTERED]


def test_interleaved_overlapping_invocation_mid_delivery_advances_new_source_without_loss(env, monkeypatch):
    _, conn, store, telemetry = env
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    bls_id = uuid4()
    item_1 = _feed_item(
        source_id=bls_id,
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        published_at=pub,
    )
    item_2 = _feed_item(
        headline="U.S. consumer prices rose 0.3% in September 2025",
        canonical_url="https://reuters.example.com/cpi-sep-2025",
        publisher="Reuters",
        source_class="reputable_secondary",
        published_at=pub + timedelta(minutes=20),
    )

    import app.scout.delivery as delivery_mod
    original_deliver = delivery_mod.deliver_candidate
    triggered = False

    def interleaved_deliver(workflow, candidate, ref, run_id, trace_id):
        nonlocal triggered
        if not triggered:
            triggered = True
            # While run_1 is mid-delivery (pending), run_2 arrives with item_1 + item_2!
            out2 = run_scout(
                _request(),
                store=store,
                telemetry=telemetry,
                fetch=lambda *_, **__: {"bls": item_1.canonical_bytes(), "reuters": item_2.canonical_bytes()},
                enrich=_enrich,
            )
            assert out2.outcome == "complete"
        return original_deliver(workflow, candidate, ref, run_id, trace_id)

    monkeypatch.setattr(delivery_mod, "deliver_candidate", interleaved_deliver)
    out1 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_, **__: {"bls": item_1.canonical_bytes()},
        enrich=_enrich,
    )
    assert out1.outcome == "complete"
    deliveries = get_deliveries(conn)
    assert [(d.revision, d.status) for d in deliveries] == [(1, "delivered"), (2, "delivered")]
    assert deliveries[0].story_id == deliveries[1].story_id
    events = scout_state(store).events(deliveries[0].story_id)
    assert [e.to_state for e in events] == [
        StoryState.DISCOVERED,
        StoryState.CLUSTERED,
        StoryState.NEEDS_REVISION,
        StoryState.CLUSTERED,
    ]


def test_offline_audit_replay_reproduces_exact_sha256_and_preserves_corrections(env, capsys):
    root, conn, store, telemetry = env
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    bls_id = uuid4()
    item_v1 = _feed_item(
        source_id=bls_id,
        revision=1,
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        published_at=pub,
        excerpt="The all-items CPI-U rose 0.4 percent in September 2025.",
    )
    run1 = run_scout(_request(), store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item_v1.canonical_bytes()}, enrich=_enrich)
    (ref_v1,) = run1.candidates
    cand_v1 = StoryCandidate.model_validate_json(store.read(ref_v1))

    item_v2 = _feed_item(
        source_id=bls_id,
        revision=2,
        headline="Correction: Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        published_at=pub,
        excerpt="Corrected: The all-items CPI-U rose 0.3 percent in September 2025.",
    )
    run2 = run_scout(_request(), store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item_v2.canonical_bytes()}, enrich=_enrich)
    (ref_v2,) = run2.candidates

    # Snapshot DB counts before audit replay to assert zero side-effects.
    counts_before = {
        tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        for tbl in ("stories", "state_events", "scout_deliveries", "scout_story_revisions", "artifacts")
    }

    r1 = replay_scout(store, story_id=cand_v1.story_id, revision=1)
    r2 = replay_scout(store, story_id=cand_v1.story_id, revision=2)
    assert r1.replay_mode == "audit_replay"
    assert r1.network_disabled is True and r1.dispatch_disabled is True
    assert r1.all_hashes_matched is True
    assert r1.candidates[0].reproduced_artifact_sha256 == ref_v1.sha256
    assert r2.all_hashes_matched is True
    assert r2.candidates[0].reproduced_artifact_sha256 == ref_v2.sha256
    assert ref_v1.sha256 != ref_v2.sha256

    counts_after = {
        tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        for tbl in ("stories", "state_events", "scout_deliveries", "scout_story_revisions", "artifacts")
    }
    assert counts_after == counts_before

    capsys.readouterr()
    assert main(["replay", "--output", str(root), "--story-id", str(cand_v1.story_id), "--revision", "1"]) == 0
    cli_out = capsys.readouterr().out
    assert "audit_replay" in cli_out
    assert ref_v1.sha256 in cli_out


def test_audit_replay_identifies_unreplayable_metadata_only_and_unknown_rights(env):
    _, conn, store, telemetry = env
    permitted = _feed_item(
        headline="Permitted CPI release",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        excerpt="The all-items CPI-U rose 0.3 percent.",
        rights="excerpt_permitted",
    )
    meta_only = _feed_item(
        headline="Metadata only wire story",
        canonical_url="https://wire.example.com/story-1",
        publisher="Wire",
        source_class="reputable_secondary",
        excerpt=None,
        rights="metadata_only",
    )
    unknown = _feed_item(
        headline="Unknown rights story",
        canonical_url="https://blog.example.com/story-2",
        publisher="Blog",
        source_class="commentary",
        excerpt=None,
        rights="unknown",
    )

    out = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_, **__: {
            "permitted": permitted.canonical_bytes(),
            "meta": meta_only.canonical_bytes(),
            "unknown": unknown.canonical_bytes(),
        },
        enrich=_enrich,
    )
    assert out.outcome == "complete"
    assert len(out.candidates) == 2  # permitted and meta_only produced live candidates

    replay = replay_scout(store, run_id=out.run_id)
    assert replay.replay_coverage.total_inputs == 3
    assert replay.replay_coverage.replayable_inputs == 1
    assert replay.replay_coverage.unreplayable_inputs == 2
    assert replay.replay_coverage.status == "partial"
    by_item = {u.item_id: u for u in replay.unreplayable_items}
    assert by_item["meta"].rights == "metadata_only"
    assert by_item["unknown"].rights == "unknown"
    assert len(replay.candidates) == 1
    assert replay.all_hashes_matched is True


def test_reeval_creates_new_run_with_own_budget_and_preserves_lineage(env, capsys):
    root, conn, store, telemetry = env
    item = _feed_item(
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
    )
    live_req = _request(model="recorded-v1", prompt_version="v1", prompt_hash="1" * 64)
    live_out = run_scout(
        live_req,
        store=store,
        telemetry=telemetry,
        fetch=lambda *_, **__: {"bls": item.canonical_bytes()},
        enrich=_enrich,
    )
    (live_ref,) = live_out.candidates
    live_cand = StoryCandidate.model_validate_json(store.read(live_ref))

    deliveries_before = get_deliveries(conn)
    revisions_before = story_revisions(store, live_cand.story_id)

    def alt_enrich(_request, members, **_kwargs):
        enrichment = ScoutEnrichment.model_validate_json(json.dumps({
            "schema_version": "1.0",
            "summary": [{"text": f"Re-evaluated summary for {members[0].headline}.", "sources": [members[0].ref.model_dump(mode="json")]}],
            "topics": ["prices"],
            "entities": ["BLS"],
            "warnings": ["seasonally_adjusted"],
        }))
        return ModelResult(enrichment.canonical_bytes(), ScoutTokenUsage(input_tokens=15, output_tokens=9))

    reeval = reeval_scout(
        store,
        source_run_id=live_req.run_id,
        model="recorded-v2",
        prompt_version="v2",
        prompt_hash="2" * 64,
        experiment_id="exp-reeval-01",
        hard_spend_cap_usd=Decimal("0.00"),
        enrich=alt_enrich,
        telemetry=telemetry,
    )
    assert reeval.replay_mode == "reeval"
    assert reeval.run_id != live_req.run_id
    assert reeval.invocation_id != live_req.invocation_id
    assert reeval.source_run_id == live_req.run_id
    assert reeval.source_input_sha256 == live_out.input_ref.sha256
    assert reeval.experiment_id == "exp-reeval-01"
    assert reeval.model == "recorded-v2"
    assert reeval.prompt_version == "v2"
    assert reeval.candidates[0].story_id == live_cand.story_id
    assert reeval.candidates[0].revision == live_cand.revision
    assert reeval.candidates[0].reproduced_artifact_sha256 != live_ref.sha256

    # Live deliveries & story revisions untouched!
    assert get_deliveries(conn) == deliveries_before
    assert story_revisions(store, live_cand.story_id) == revisions_before

    # Experiment budget registered independently in SpendLedger.
    summary = SpendLedger(conn).summary("exp-reeval-01")
    assert summary.experiment_id == "exp-reeval-01"

    # CLI reeval subcommand with recording works offline.
    rec_path = root / "reeval-recording.json"
    rec_payload = {
        "schema_version": "1.0",
        "feed": {"items": {"bls": json.loads(item.canonical_bytes().decode("utf-8"))}},
        "responses": {
            item.source.canonical_url: {
                "usage": {"input_tokens": 14, "output_tokens": 7},
                "output": {
                    "schema_version": "1.0",
                    "summary": [{"text": "CLI re-evaluated summary.", "sources": [live_cand.representative.model_dump(mode="json")]}],
                    "topics": ["prices"],
                    "entities": ["BLS"],
                    "warnings": ["seasonally_adjusted"],
                },
            }
        },
    }
    rec_path.write_text(json.dumps(rec_payload), encoding="utf-8")
    capsys.readouterr()
    assert main([
        "reeval",
        "--output", str(root),
        "--run-id", str(live_req.run_id),
        "--model", "recorded-v3",
        "--prompt-version", "v3",
        "--experiment-id", "exp-cli-02",
        "--recording", str(rec_path),
    ]) == 0
    cli_out = capsys.readouterr().out
    assert "reeval" in cli_out
    assert "exp-cli-02" in cli_out
    assert "recorded-v3" in cli_out
    assert str(live_req.run_id) in cli_out

    # JSON output mode parses cleanly into strict contracts and distinguishes modes.
    from app.scout.replay import ScoutAuditReplayOutput, ScoutReevalOutput
    assert main(["replay", "--output", str(root), "--run-id", str(live_req.run_id), "--json"]) == 0
    audit_json = ScoutAuditReplayOutput.model_validate_json(capsys.readouterr().out)
    assert audit_json.replay_mode == "audit_replay"

    assert main([
        "reeval",
        "--output", str(root),
        "--run-id", str(live_req.run_id),
        "--model", "recorded-v4",
        "--experiment-id", "exp-cli-json",
        "--recording", str(rec_path),
        "--json",
    ]) == 0
    reeval_json = ScoutReevalOutput.model_validate_json(capsys.readouterr().out)
    assert reeval_json.replay_mode == "reeval"
    assert reeval_json.source_run_id == live_req.run_id


def test_crash_during_rev2_rework_and_rejected_recovery_failure(env, monkeypatch):
    _, conn, store, telemetry = env
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    bls_id = uuid4()
    item_v1 = _feed_item(
        source_id=bls_id,
        revision=1,
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        published_at=pub,
        excerpt="The all-items CPI-U rose 0.4 percent in September 2025.",
    )
    run1 = run_scout(_request(), store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item_v1.canonical_bytes()}, enrich=_enrich)
    cand_v1 = StoryCandidate.model_validate_json(store.read(run1.candidates[0]))

    item_v2 = _feed_item(
        source_id=bls_id,
        revision=2,
        headline="Correction: Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        published_at=pub,
        excerpt="Corrected: The all-items CPI-U rose 0.3 percent in September 2025.",
    )

    import app.scout.delivery as delivery_mod
    original_deliver = delivery_mod.deliver_candidate

    def crash_after_needs_revision(workflow, candidate, ref, run_id, trace_id):
        snap = workflow.get(candidate.story_id)
        workflow.transition(TransitionRequest(
            story_id=candidate.story_id,
            target=StoryState.NEEDS_REVISION,
            expected_version=snap.version,
            caller_scope="news_scout",
            idempotency_key=f"{ref.sha256}:needs_revision",
            reason_code="source_correction",
            trace_id=trace_id,
        ))
        raise SimulatedCrash("crash mid rev2 rework")

    from app.workflow.state import TransitionRequest
    monkeypatch.setattr(delivery_mod, "deliver_candidate", crash_after_needs_revision)
    with pytest.raises(SimulatedCrash):
        run_scout(_request(), store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item_v2.canonical_bytes()}, enrich=_enrich)

    assert scout_state(store).get(cand_v1.story_id).state == StoryState.NEEDS_REVISION
    monkeypatch.setattr(delivery_mod, "deliver_candidate", original_deliver)
    recovered = recover_pending_deliveries(store)
    assert len(recovered) == 1
    assert recovered[0].revision == 2 and recovered[0].status == "delivered"
    assert scout_state(store).get(cand_v1.story_id).state == StoryState.CLUSTERED
    assert len(story_revisions(store, cand_v1.story_id)) == 2

    # If a pending rev 3 conflicts because story was rejected before recovery, recovery marks it failed.
    snap = scout_state(store).get(cand_v1.story_id)
    scout_state(store).transition(TransitionRequest(
        story_id=cand_v1.story_id,
        target=StoryState.REJECTED,
        expected_version=snap.version,
        caller_scope="test",
        idempotency_key="reject-before-rev3",
        reason_code="editorial_reject",
    ))
    item_v3 = _feed_item(
        source_id=bls_id,
        revision=3,
        headline="Second Correction: CPI September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        published_at=pub,
        excerpt="Corrected again: 0.35 percent.",
    )
    def crash_before_dispatch(workflow, candidate, ref, run_id, trace_id):
        raise SimulatedCrash("crash before rev3 dispatch")
    monkeypatch.setattr(delivery_mod, "deliver_candidate", crash_before_dispatch)
    with pytest.raises(SimulatedCrash):
        run_scout(_request(), store=store, telemetry=telemetry, fetch=lambda *_, **__: {"bls": item_v3.canonical_bytes()}, enrich=_enrich)
    monkeypatch.setattr(delivery_mod, "deliver_candidate", original_deliver)
    failed_recovery = recover_pending_deliveries(store)
    assert len(failed_recovery) == 1
    assert failed_recovery[0].status == "failed"
    assert failed_recovery[0].error_code == "revision_conflict"


def test_audit_replay_unavailable_when_all_inputs_metadata_only(env):
    _, _, store, telemetry = env
    meta_only = _feed_item(
        headline="Metadata only wire story",
        canonical_url="https://wire.example.com/only-meta",
        publisher="Wire",
        source_class="reputable_secondary",
        excerpt=None,
        rights="metadata_only",
    )
    out = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_, **__: {"meta": meta_only.canonical_bytes()},
        enrich=_enrich,
    )
    assert out.outcome == "complete"
    replay = replay_scout(store, run_id=out.run_id)
    assert replay.replay_coverage.status == "unavailable"
    assert replay.replay_coverage.replayable_inputs == 0
    assert replay.replay_coverage.unreplayable_inputs == 1
    assert replay.candidates == ()
    assert replay.all_hashes_matched is False
    assert replay.unreplayable_items[0].rights == "metadata_only"
