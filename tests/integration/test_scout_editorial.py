"""Integration tests for recorded Scout editorial assignments, pilot cost reporting, CLI, and UI."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import httpx
import pytest

from app.contracts.scout import ScoutRunInput, ScoutRunOutput, StoryCandidate
from app.contracts.workflow import StoryState
from app.observability.tracing import Telemetry
from app.scout.cli import ScoutRecording
from app.scout.editorial import (
    ExportGateError,
    RunCostAttempt,
    export_adjudicated_failures,
    generate_pilot_report,
    list_candidates,
    record_decision,
    record_pilot_run,
)
from app.scout.editorial_cli import main as editorial_main
from app.scout.ledger import SpendLedger
from app.scout.registry import FeedRegistry, FeedSettings
from app.scout.rss import RSSPoller
from app.scout.run import ModelResult, run_scout
from app.scout.workflow import scout_state
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database
from app.ui.scout_editorial import editorial_overview

RECORDING = Path(__file__).parents[1] / "fixtures" / "scout" / "bls-cpi.json"
REQUEST_FIXTURE = Path(__file__).parents[1] / "fixtures" / "scout" / "bls-cpi-request.json"


@pytest.fixture(autouse=True)
def _windows_dir_fsync_compat(monkeypatch):
    """On Windows, os.open on a directory raises PermissionError; skip only directory-fd fsync."""
    if os.name != "nt":
        return
    import app.storage.artifacts as artifacts_mod

    real_open = os.open
    real_fsync = os.fsync
    real_close = os.close
    sentinel_fds: set[int] = set()
    next_fd = 910_000

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
            if fd in sentinel_fds:
                return
            real_fsync(fd)

        def close(self, fd):
            if fd in sentinel_fds:
                sentinel_fds.discard(fd)
                return
            real_close(fd)

    monkeypatch.setattr(artifacts_mod, "os", _WinSafeOs())


def _recorded_request(**overrides) -> ScoutRunInput:
    fields = json.loads(REQUEST_FIXTURE.read_bytes())
    fields.update(run_id=str(uuid4()), invocation_id=str(uuid4()))
    return ScoutRunInput.model_validate_json(json.dumps(fields | overrides))


def _rss_xml(title: str, items: list[tuple[str, str, str, str]]) -> str:
    entries = "\n".join(
        f"<item><title>{headline}</title><link>{link}</link>"
        f"<pubDate>{pub_date}</pubDate><description>{desc}</description></item>"
        for headline, link, pub_date, desc in items
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{title}</title>{entries}</channel></rss>'


def test_end_to_end_recorded_flow_inspection_to_pilot_report(tmp_path):
    request = _recorded_request()
    recording = ScoutRecording.model_validate_json(RECORDING.read_bytes())

    db_path = tmp_path / "scout.sqlite3"
    with closing(open_database(db_path)) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "artifacts", conn)
        telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
        try:
            run_output = run_scout(
                request,
                store=store,
                telemetry=telemetry,
                fetch=recording.fetch,
                enrich=recording.enrich,
            )
        finally:
            telemetry.shutdown(timeout_seconds=0)

        assert run_output.outcome == "complete"
        (candidate_ref,) = run_output.candidates

        # Step 1: Editor inspects produced candidates (initially pending).
        candidates = list_candidates(conn, store, run_id=run_output.run_id)
        assert len(candidates) == 1
        item = candidates[0]
        assert item.status == "pending"
        assert item.candidate_sha256 == candidate_ref.sha256
        assert "Consumer Price Index" in item.headline

        # Step 2: Record pilot run metadata + recorded measured model cost.
        record_pilot_run(
            conn,
            run_output=run_output,
            store=store,
            recorded_at=request.window_end,
            feed_ids=("bls-cpi",),
            cost_records=(
                RunCostAttempt(
                    attempt_id="recorded-cpi-1",
                    cost_kind="measured",
                    cost_usd=Decimal("0.00042"),
                    input_tokens=run_output.reported_tokens.input_tokens,
                    output_tokens=run_output.reported_tokens.output_tokens,
                    model_id=request.configuration.model,
                ),
            ),
        )

        # Step 3: Editor records an editorial assignment label.
        decision = record_decision(
            conn,
            story_id=item.story_id,
            revision=item.revision,
            run_id=run_output.run_id,
            candidate_sha256=item.candidate_sha256,
            decision="assigned",
            reason_code="primary_cpi_release",
            notes="Assign to morning macro desk.",
            actor_id="editor-1",
        )
        assert decision.decision == "assigned"

        # Workflow publication state must remain untouched at CLUSTERED.
        assert scout_state(store).get(item.story_id).state == StoryState.CLUSTERED
        assert conn.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0] == 0

        # Step 4: Operator generates pilot report.
        report = generate_pilot_report(conn)
        assert report.channel_totals.total_runs == 1
        assert report.channel_totals.assignment_producing_runs == 1
        assert report.channel_totals.distinct_assigned == 1
        assert report.channel_totals.pending_stories == 0
        assert report.cost.measured_cost_usd == Decimal("0.00042")
        assert report.cost.cost_per_distinct_assignment_usd == Decimal("0.00042")


def test_multi_run_rss_pilot_overlap_empty_failed_correction_and_ledger(tmp_path):
    from app.contracts.scout import AttributedStatement, ScoutEnrichment, ScoutTokenUsage

    db_path = tmp_path / "scout.sqlite3"
    ledger_path = tmp_path / "ledger.sqlite3"
    with closing(open_database(ledger_path)) as lconn:
        migrate(lconn)
    ledger = SpendLedger(ledger_path)
    ledger.ensure_experiment("pilot-rss", hard_spend_cap_usd=Decimal("0.50"), config_hash="d" * 64)

    with closing(open_database(db_path)) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "artifacts", conn)
        telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
        registry = FeedRegistry(conn)
        registry.seed()
        registry.add(
            "wire-macro",
            FeedSettings(
                endpoint="https://wire.example.org/macro.rss",
                publisher="Macro Wire",
                source_class="reputable_secondary",
                rights="metadata_only",
                usage_notes="Syndicated macro metadata.",
            ),
            actor="curator",
            reason="Add secondary wire feed",
        )
        registry.revise("bls-employment-situation", changes={"enabled": False}, actor="curator", reason="Focus test")
        snapshot = registry.snapshot()

        t_base = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        round_num = 0

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if round_num == 1:
                if "bls.gov" in url:
                    return httpx.Response(200, text=_rss_xml("BLS CPI", [
                        ("Consumer Price Index - August 2025", "https://www.bls.gov/cpi-aug-2025.htm",
                         "Thu, 11 Sep 2025 12:30:00 GMT", "CPI release."),
                        ("Unemployment Insurance Weekly Claims - September 11, 2025", "https://www.bls.gov/claims-sep-2025.htm",
                         "Thu, 11 Sep 2025 13:00:00 GMT", "Weekly jobless claims details."),
                    ]))
                if "wire.example.org" in url:
                    return httpx.Response(200, text=_rss_xml("Macro Wire", [
                        ("US Consumer Price Index rises in August 2025", "https://wire.example.org/cpi-aug-2025",
                         "Thu, 11 Sep 2025 12:45:00 GMT", "Wire report on CPI."),
                    ]))
                return httpx.Response(200, text=_rss_xml("Fed", []))
            if round_num == 2:
                return httpx.Response(200, text=_rss_xml("Empty Feed", []))
            if round_num == 3:
                return httpx.Response(503)
            # Round 4: correction on CPI from wire-macro
            if "wire.example.org" in url:
                return httpx.Response(200, text=_rss_xml("Macro Wire", [
                    ("Correction: US Consumer Price Index August 2025", "https://wire.example.org/cpi-aug-2025?rev=2",
                     "Thu, 11 Sep 2025 14:00:00 GMT", "Corrected CPI release."),
                ]))
            return httpx.Response(200, text=_rss_xml("Feed", []))

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            def enrich_ok(_req, members, **_kw):
                enrichment = ScoutEnrichment(
                    schema_version="1.0",
                    topics=("prices",),
                    warnings=(),
                    entities=("Consumer Price Index",),
                    summary=(AttributedStatement(
                        text="Official CPI bulletin.",
                        sources=tuple(m.ref for m in members),
                    ),),
                )
                return ModelResult(enrichment.canonical_bytes(), ScoutTokenUsage(input_tokens=100, output_tokens=50))

            # Run 1: multi-feed overlap on CPI + second BLS story + quiet Fed feed.
            round_num = 1
            req1 = _recorded_request(limits={"max_items": 10, "max_tokens": 2000, "timeout_seconds": 10})
            req1 = req1.model_copy(update={"configuration": req1.configuration.model_copy(update={"feed_snapshot": snapshot.record_hash()})})
            out1 = run_scout(req1, store=store, telemetry=telemetry, fetch=RSSPoller(registry, snapshot, client, now=lambda: t_base), enrich=enrich_ok)
            res1 = ledger.reserve(experiment_id="pilot-rss", profile_name="paid", model_id="m", example_id="r1", attempt_key="r1:0", worst_case_usd=Decimal("0.01"))
            ledger.reconcile(res1.reservation_id, actual_usd=Decimal("0.0020"), input_tokens=200, output_tokens=100)
            record_pilot_run(conn, run_output=out1, store=store, recorded_at=t_base, reservation_ids=(res1.reservation_id,))

            # Run 2: empty run (304 Not Modified).
            round_num = 2
            req2 = _recorded_request()
            req2 = req2.model_copy(update={"configuration": req2.configuration.model_copy(update={"feed_snapshot": snapshot.record_hash()})})
            out2 = run_scout(req2, store=store, telemetry=telemetry, fetch=RSSPoller(registry, snapshot, client, now=lambda: t_base + timedelta(minutes=10)), enrich=enrich_ok)
            assert out2.outcome == "empty"
            record_pilot_run(conn, run_output=out2, store=store, recorded_at=t_base + timedelta(minutes=10))

            # Run 3: failed run (503 all unavailable) with an ambiguous reservation.
            round_num = 3
            req3 = _recorded_request()
            req3 = req3.model_copy(update={"configuration": req3.configuration.model_copy(update={"feed_snapshot": snapshot.record_hash()})})
            out3 = run_scout(req3, store=store, telemetry=telemetry, fetch=RSSPoller(registry, snapshot, client, now=lambda: t_base + timedelta(minutes=20)), enrich=enrich_ok)
            assert out3.outcome == "failed"
            res3 = ledger.reserve(experiment_id="pilot-rss", profile_name="paid", model_id="m", example_id="r3", attempt_key="r3:0", worst_case_usd=Decimal("0.005"))
            ledger.mark_ambiguous(res3.reservation_id, failure_disposition="http_error")
            record_pilot_run(conn, run_output=out3, store=store, recorded_at=t_base + timedelta(minutes=20), reservation_ids=(res3.reservation_id,))

            # Run 4: correction (rev 2) on CPI story.
            round_num = 4
            req4 = _recorded_request()
            req4 = req4.model_copy(update={"configuration": req4.configuration.model_copy(update={"feed_snapshot": snapshot.record_hash()})})
            out4 = run_scout(req4, store=store, telemetry=telemetry, fetch=RSSPoller(registry, snapshot, client, now=lambda: t_base + timedelta(minutes=30)), enrich=enrich_ok)
            record_pilot_run(conn, run_output=out4, store=store, recorded_at=t_base + timedelta(minutes=30))
        finally:
            client.close()
            telemetry.shutdown(timeout_seconds=0)

        r1_cands = list_candidates(conn, store, run_id=out1.run_id)
        cpi_r1 = next(c for c in r1_cands if "Consumer Price Index" in c.headline)
        claims_r1 = next(c for c in r1_cands if c.story_id != cpi_r1.story_id)
        r4_cands = list_candidates(conn, store, run_id=out4.run_id)
        cpi_r2 = next(c for c in r4_cands if c.story_id == cpi_r1.story_id)
        assert cpi_r2.revision == 2

        # Assign CPI rev 1 in R1, pass claims in R1, and assign CPI rev 2 again in R4.
        record_decision(
            conn, story_id=cpi_r1.story_id, revision=1, run_id=out1.run_id,
            candidate_sha256=cpi_r1.candidate_sha256, decision="assigned",
            reason_code="macro_cpi", notes="", actor_id="ed-1", decided_at=t_base + timedelta(minutes=2),
        )
        record_decision(
            conn, story_id=claims_r1.story_id, revision=1, run_id=out1.run_id,
            candidate_sha256=claims_r1.candidate_sha256, decision="passed",
            reason_code="too_narrow", notes="Weekly claims noise.", actor_id="ed-1",
            decided_at=t_base + timedelta(minutes=3),
        )
        record_decision(
            conn, story_id=cpi_r2.story_id, revision=2, run_id=out4.run_id,
            candidate_sha256=cpi_r2.candidate_sha256, decision="assigned",
            reason_code="correction_confirmed", notes="", actor_id="ed-1",
            decided_at=t_base + timedelta(minutes=35),
        )

    with closing(open_database(db_path)) as conn, closing(open_database(ledger_path)) as lconn:
        report = generate_pilot_report(conn, ledger_conn=lconn)
        assert report.channel_totals.total_runs == 4
        assert report.channel_totals.empty_runs == 1
        assert report.channel_totals.failed_runs == 1
        assert report.channel_totals.assignment_producing_runs == 1
        assert report.channel_totals.total_candidates == 2
        assert report.channel_totals.distinct_assigned == 1
        assert report.channel_totals.distinct_passed == 1
        assert report.channel_totals.pending_stories == 0
        assert report.cost.measured_cost_usd == Decimal("0.0020")
        assert report.cost.missing_cost_attempts == 1
        assert report.cost.cost_per_distinct_assignment_usd == Decimal("0.002")

        by_feed = {row.feed_id: row for row in report.by_feed}
        assert by_feed["bls-cpi"].unique_candidates == 2
        assert by_feed["bls-cpi"].unique_assignments == 1
        assert by_feed["wire-macro"].unique_candidates == 1
        assert by_feed["wire-macro"].unique_assignments == 1
        assert by_feed["fed-monetary-policy"].unique_candidates == 0

        # Export adjudicated failures (only the passed regional story).
        export_path = tmp_path / "failures.json"
        exported = export_adjudicated_failures(
            conn,
            ArtifactStore(tmp_path / "artifacts", conn),
            output_path=export_path,
            rights_cleared=True,
            privacy_checked=True,
            dataset_version="1.1.0-draft",
        )
        assert exported.record_count == 1
        assert exported.records[0].story_id == claims_r1.story_id
        assert "ed-1" not in export_path.read_text(encoding="utf-8")


def test_editorial_cli_and_readonly_ui(tmp_path, capsys):
    request = _recorded_request()
    recording = ScoutRecording.model_validate_json(RECORDING.read_bytes())

    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "artifacts", conn)
        telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
        try:
            run_output = run_scout(
                request,
                store=store,
                telemetry=telemetry,
                fetch=recording.fetch,
                enrich=recording.enrich,
            )
        finally:
            telemetry.shutdown(timeout_seconds=0)

    (tmp_path / "report.json").write_bytes(run_output.canonical_bytes())

    assert editorial_main(["inspect", "--output", str(tmp_path), "--run-id", str(run_output.run_id)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert len(inspected) == 1
    cand = inspected[0]
    assert cand["status"] == "pending"

    assert editorial_main([
        "record-run", "--output", str(tmp_path),
        "--report", str(tmp_path / "report.json"),
        "--feed-ids", "bls-cpi",
        "--recorded-at", request.window_end.isoformat(),
    ]) == 0
    capsys.readouterr()

    assert editorial_main([
        "decide", "--output", str(tmp_path),
        "--story-id", cand["story_id"],
        "--revision", str(cand["revision"]),
        "--run-id", cand["run_id"],
        "--candidate-sha256", cand["candidate_sha256"],
        "--decision", "passed",
        "--reason-code", "duplicate_topic",
        "--notes", "Already covered elsewhere.",
        "--actor-id", "editor-cli",
    ]) == 0
    dec_out = json.loads(capsys.readouterr().out)
    assert dec_out["decision"] == "passed"

    assert editorial_main(["report", "--output", str(tmp_path)]) == 0
    rep_out = json.loads(capsys.readouterr().out)
    assert rep_out["channel_totals"]["distinct_assigned"] == 0
    assert rep_out["cost"]["cost_per_distinct_assignment_usd"] is None

    # Export without gates must fail non-zero.
    export_file = tmp_path / "cli_failures.json"
    assert editorial_main(["export-failures", "--output", str(tmp_path), "--out", str(export_file)]) != 0
    capsys.readouterr()
    assert not export_file.exists()

    # Export with both gates succeeds.
    assert editorial_main([
        "export-failures", "--output", str(tmp_path),
        "--out", str(export_file),
        "--rights-cleared", "--privacy-checked",
    ]) == 0
    capsys.readouterr()
    assert export_file.exists()

    # Read-only UI overview works and never creates a missing database.
    overview = editorial_overview(tmp_path / "scout.sqlite3")
    assert overview["boundary_notice"]
    assert overview["report"]["channel_totals"]["distinct_passed"] == 1
    assert overview["candidates"][0]["status"] == "passed"

    missing_db = tmp_path / "does_not_exist.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        editorial_overview(missing_db)
    assert not missing_db.exists()
