"""Unit tests for Scout operational monitoring repository and dashboard view."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
from uuid import uuid4

import pytest

from app.contracts.common import ArtifactRef
from app.contracts.evidence import SourceRecord
from app.contracts.scout import (
    AttributedStatement,
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
from app.monitoring.scout_ops import ScoutOperationalRepository
from app.monitoring.scout_ui import build_dashboard_view
from app.observability.models import CollectorHealth, SpanRecord
from app.observability.tracing import Telemetry
from app.scout.ledger import SpendLedger
from app.scout.registry import FeedRegistry, FeedSettings
from app.storage.db import migrate, open_database

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _init_scout_db(tmp_path):
    db_path = tmp_path / "scout.sqlite3"
    with closing(open_database(db_path)) as conn:
        migrate(conn)
    return db_path


def _seed_feed(db_path, feed_id: str = "bls-cpi", source_class: str = "primary"):
    with closing(open_database(db_path)) as conn:
        reg = FeedRegistry(conn)
        rev = reg.add(
            feed_id,
            FeedSettings(
                endpoint=f"https://example.com/{feed_id}.rss",
                publisher="Test Publisher",
                source_class=source_class,
                rights="metadata_only",
                usage_notes="Metadata only",
            ),
            actor="curator",
            reason="initial",
        )
        return rev


def _seed_cache(db_path, feed_id: str, revision: int, retrieved_at: datetime):
    body = json.dumps(
        {
            "schema_version": "1.0",
            "etag": '"v1"',
            "last_modified": None,
            "retrieved_at": retrieved_at.isoformat(),
            "items": {},
            "problems": {},
        }
    )
    with closing(open_database(db_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO scout_feed_cache(feed_id, revision, body) VALUES (?, ?, ?)",
                (feed_id, revision, body),
            )


def _span(
    *,
    trace_id: str,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    name: str,
    run_id=None,
    story_id=None,
    start_ns: int = 1_000_000_000,
    duration_ms: float = 10.0,
    status: str = "OK",
    attributes: dict | None = None,
) -> SpanRecord:
    return SpanRecord(
        trace_id=trace_id,
        span_id=span_id or uuid4().hex[:16],
        parent_span_id=parent_span_id,
        project="finance-media-news-scout",
        name=name,
        start_time_ns=start_ns,
        end_time_ns=start_ns + int(duration_ms * 1_000_000),
        run_id=run_id,
        story_id=story_id,
        status=status,
        attributes=attributes or {},
    )


def test_missing_databases_and_readonly_enforcement(tmp_path):
    missing_db = tmp_path / "missing-scout.sqlite3"
    missing_tel = tmp_path / "missing-telemetry.sqlite3"
    repo = ScoutOperationalRepository(missing_db, telemetry_path=missing_tel, now=lambda: NOW)

    summary = repo.snapshot()
    assert not missing_db.exists()
    assert not missing_tel.exists()
    assert summary.feeds == ()
    assert summary.item_rates.status == "unknown"
    assert summary.item_rates.invalid_rate is None
    assert summary.item_rates.stale_rate is None
    assert summary.item_rates.availability_reason is not None
    assert summary.source_mix.status == "unknown"
    assert summary.source_mix.shares is None
    assert summary.duplicate_quality.status == "unknown"
    assert summary.duplicate_quality.precision is None
    assert summary.duplicate_quality.recall is None
    assert summary.duplicate_quality.availability_reason == "unlabeled_production_traffic"
    assert summary.budget.status == "unknown"
    assert summary.budget.reconciled_cost_usd is None
    assert summary.handoffs.status == "unknown"
    assert summary.handoffs.pending_count is None
    assert summary.handoffs.availability_reason == "scout_deliveries_table_unavailable"
    assert summary.telemetry.status == "unknown"
    assert summary.telemetry.pending_spans is None

    db_path = _init_scout_db(tmp_path)
    ro_repo = ScoutOperationalRepository(db_path)
    with ro_repo.connection(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM scout_run_requests")


def test_feed_freshness_item_rates_source_mix_latency_and_retries(tmp_path):
    db_path = _init_scout_db(tmp_path)
    _seed_feed(db_path, "bls-cpi", "primary")
    _seed_feed(db_path, "secondary-wire", "reputable_secondary")
    _seed_cache(db_path, "bls-cpi", 1, NOW - timedelta(minutes=15))

    tel_path = tmp_path / "telemetry.sqlite3"
    telemetry = Telemetry(tel_path)
    run_id = uuid4()
    trace_id = uuid4().hex
    root_span_id = uuid4().hex[:16]

    spans = [
        _span(
            trace_id=trace_id,
            span_id=root_span_id,
            name="agent.invoke",
            run_id=run_id,
            duration_ms=120.0,
            attributes={"retry.count": 1, "run.outcome": "partial", "result.status": "BLOCKED"},
        ),
        _span(
            trace_id=trace_id,
            parent_span_id=root_span_id,
            name="scout.fetch",
            run_id=run_id,
            duration_ms=25.0,
            attributes={"feed.id": "bls-cpi", "feed.revision": 1, "feed.status": "read", "source.count": 3},
        ),
        _span(
            trace_id=trace_id,
            parent_span_id=root_span_id,
            name="scout.fetch",
            run_id=run_id,
            duration_ms=50.0,
            status="ERROR",
            attributes={
                "feed.id": "secondary-wire",
                "feed.revision": 1,
                "feed.status": "unavailable",
                "feed.reason": "timeout",
                "source.count": 0,
            },
        ),
        _span(
            trace_id=trace_id,
            parent_span_id=root_span_id,
            name="scout.item",
            run_id=run_id,
            duration_ms=10.0,
            attributes={"item.id": "item-1", "item.status": "new", "item.reason": "candidate_created", "source.class": "primary"},
        ),
        _span(
            trace_id=trace_id,
            parent_span_id=root_span_id,
            name="scout.item",
            run_id=run_id,
            duration_ms=5.0,
            attributes={"item.id": "item-2", "item.status": "withheld", "item.reason": "outside_window", "source.class": "reputable_secondary"},
        ),
        _span(
            trace_id=trace_id,
            parent_span_id=root_span_id,
            name="scout.item",
            run_id=run_id,
            duration_ms=5.0,
            status="ERROR",
            attributes={"item.id": "item-3", "item.status": "failed", "item.reason": "invalid_provenance", "source.class": "commentary"},
        ),
        _span(
            trace_id=trace_id,
            parent_span_id=root_span_id,
            name="scout.item",
            run_id=run_id,
            duration_ms=5.0,
            status="ERROR",
            attributes={"item.id": "item-4", "item.status": "failed", "item.reason": "malformed_feed_item", "source.class": "unknown"},
        ),
        _span(
            trace_id=trace_id,
            parent_span_id=root_span_id,
            name="model.call",
            run_id=run_id,
            duration_ms=40.0,
            attributes={"model.provider": "openrouter", "model.name": "fake", "retry.count": 2},
        ),
    ]
    for s in spans:
        assert telemetry.enqueue(s).accepted
    telemetry.shutdown(0)

    repo = ScoutOperationalRepository(db_path, telemetry_path=tel_path, now=lambda: NOW)
    summary = repo.snapshot()

    feeds = {f.feed_id: f for f in summary.feeds}
    assert feeds["bls-cpi"].freshness_age_seconds == pytest.approx(900.0)
    assert feeds["bls-cpi"].poll_count == 1
    assert feeds["bls-cpi"].failure_count == 0
    assert feeds["bls-cpi"].failure_rate == pytest.approx(0.0)

    assert feeds["secondary-wire"].last_retrieved_at is None
    assert feeds["secondary-wire"].freshness_age_seconds is None
    assert feeds["secondary-wire"].poll_count == 1
    assert feeds["secondary-wire"].failure_count == 1
    assert feeds["secondary-wire"].failure_rate == pytest.approx(1.0)
    assert feeds["secondary-wire"].failure_reasons == {"timeout": 1}

    assert summary.item_rates.status == "measured"
    assert summary.item_rates.total_items == 4
    assert summary.item_rates.invalid_count == 2
    assert summary.item_rates.stale_count == 1
    assert summary.item_rates.invalid_rate == pytest.approx(0.5)
    assert summary.item_rates.stale_rate == pytest.approx(0.25)

    assert summary.source_mix.status == "measured"
    assert summary.source_mix.counts == {
        "primary": 1,
        "reputable_secondary": 1,
        "commentary": 1,
        "unknown": 1,
    }
    assert summary.source_mix.shares["primary"] == pytest.approx(0.25)

    assert summary.latency.status == "measured"
    assert summary.latency.sample_count == 8
    assert summary.latency.p50_ms == pytest.approx(10.0)
    assert summary.latency.p95_ms == pytest.approx(120.0)
    assert summary.retries.total_retries == 3
    assert summary.quarantine.quarantine_count == 2
    assert summary.quarantine.reasons == {"invalid_provenance": 1, "malformed_feed_item": 1}


def test_duplicate_quality_labeled_vs_unlabeled_and_budget_usage(tmp_path):
    db_path = _init_scout_db(tmp_path)
    ledger_path = tmp_path / "ledger.sqlite3"
    with closing(open_database(ledger_path)) as conn:
        migrate(conn)
    ledger = SpendLedger(ledger_path)
    ledger.ensure_experiment("exp-1", hard_spend_cap_usd=Decimal("2.00"), config_hash="a" * 64)

    r1 = ledger.reserve(
        experiment_id="exp-1",
        profile_name="free",
        model_id="m1",
        example_id="ex1",
        attempt_key="k1",
        worst_case_usd=Decimal("0.20"),
    )
    ledger.reconcile(r1.reservation_id, actual_usd=Decimal("0.05"), input_tokens=100, output_tokens=50)

    r2 = ledger.reserve(
        experiment_id="exp-1",
        profile_name="free",
        model_id="m1",
        example_id="ex2",
        attempt_key="k2",
        worst_case_usd=Decimal("0.30"),
    )
    ledger.mark_ambiguous(r2.reservation_id, failure_disposition="timeout")

    ledger.reserve(
        experiment_id="exp-1",
        profile_name="free",
        model_id="m1",
        example_id="ex3",
        attempt_key="k3",
        worst_case_usd=Decimal("0.10"),
    )

    eval_report = tmp_path / "eval_report.json"
    eval_report.write_text(
        json.dumps(
            {
                "split": "dev",
                "dataset_version": "1.0.0",
                "experiment_sha256": "b" * 64,
                "metrics": {"duplicate_precision": 0.96, "duplicate_recall": 0.90},
                "duplicate_pairs": {"correct": 24, "predicted": 25, "expected": 27},
            }
        )
    )

    repo = ScoutOperationalRepository(
        db_path,
        ledger_path=ledger_path,
        eval_report_paths=(eval_report,),
        now=lambda: NOW,
    )
    summary = repo.snapshot()

    assert summary.duplicate_quality.status == "measured"
    assert summary.duplicate_quality.precision == pytest.approx(0.96)
    assert summary.duplicate_quality.recall == pytest.approx(24 / 27)
    assert summary.duplicate_quality.correct_pairs == 24
    assert summary.duplicate_quality.predicted_pairs == 25
    assert summary.duplicate_quality.expected_pairs == 27

    assert summary.budget.status == "measured"
    assert summary.budget.reconciled_cost_usd == Decimal("0.05")
    assert summary.budget.pending_reserved_usd == Decimal("0.10")
    assert summary.budget.ambiguous_reserved_usd == Decimal("0.30")
    assert summary.budget.encumbered_usd == Decimal("0.45")
    assert summary.budget.pending_count == 1
    assert summary.budget.ambiguous_count == 1
    assert summary.budget.reconciled_count == 1
    assert summary.budget.input_tokens == 100
    assert summary.budget.output_tokens == 50
    assert summary.quarantine.reasons.get("timeout") == 1


def test_scout_deliveries_defensive_schema_and_telemetry_outage_view(tmp_path):
    db_path = _init_scout_db(tmp_path)
    with closing(open_database(db_path)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scout_deliveries (
                    story_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    cluster_id TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    invocation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    replay_mode TEXT NOT NULL DEFAULT 'live',
                    source_run_id TEXT,
                    trace_id TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (story_id, revision)
                )
                """
            )
            for digest in ("a" * 64, "b" * 64, "c" * 64):
                conn.execute("INSERT OR IGNORE INTO artifacts VALUES (?, 'application/json', 10, '1.0')", (digest,))
            now_iso = NOW.isoformat()
            run_id = str(uuid4())
            conn.execute(
                "INSERT INTO scout_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), 1, str(uuid4()), "a" * 64, run_id, str(uuid4()), "pending", "live", None, "1" * 32, None, now_iso, now_iso),
            )
            conn.execute(
                "INSERT INTO scout_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), 1, str(uuid4()), "b" * 64, run_id, str(uuid4()), "failed", "reeval", run_id, "2" * 32, "state_conflict", now_iso, now_iso),
            )
            conn.execute(
                "INSERT INTO scout_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), 1, str(uuid4()), "c" * 64, run_id, str(uuid4()), "delivered", "live", None, "3" * 32, None, now_iso, now_iso),
            )

    tel_path = tmp_path / "telemetry.sqlite3"
    telemetry = Telemetry(tel_path)
    telemetry.enqueue(_span(trace_id="1" * 32, name="agent.invoke", run_id=UUID(run_id)))
    telemetry.outbox.failure("export_failed", (UUID(run_id),))
    telemetry.shutdown(0)

    repo = ScoutOperationalRepository(
        db_path,
        telemetry_path=tel_path,
        phoenix_probe=lambda: CollectorHealth(available=False, error_code="unavailable"),
        now=lambda: NOW,
    )
    summary = repo.snapshot(probe_collector=True)
    assert summary.handoffs.status == "measured"
    assert summary.handoffs.pending_count == 1
    assert summary.handoffs.failed_count == 1
    assert summary.handoffs.undelivered_count == 2
    assert summary.handoffs.delivered_count == 1
    assert summary.handoffs.error_counts == {"state_conflict": 1}

    assert summary.telemetry.phoenix_outage is True
    assert summary.telemetry.pending_spans == 1
    assert summary.telemetry.delivered_spans == 0
    assert any(alert.code == "phoenix_outage" for alert in summary.alerts)

    view = build_dashboard_view(summary)
    assert view["phoenix_outage"] is True
    assert view["handoffs"]["undelivered_count"] == 2
    assert view["duplicate_quality"]["status"] == "unknown"


def test_scout_sanitization_and_ui_rendering(tmp_path, monkeypatch):
    from app.monitoring.scout_ui import render_scout_dashboard
    from app.observability.sanitize import sanitize_attributes

    safe = sanitize_attributes(
        {
            "feed.id": "bls-cpi",
            "feed.revision": 2,
            "feed.status": "read",
            "item.id": "bls-cpi:abc123",
            "item.status": "new",
            "item.reason": "candidate_created",
            "source.class": "primary",
            "story.revision": 1,
            "delivery.status": "new",
            "release.id": str(uuid4()),
            "source.refs": ["a" * 64, "https://bad-url.example/secret"],
            "run.outcome": "complete",
            "OPENROUTER_API_KEY": "sk-or-v1-secret",
            "reviewer.notes": "Private reviewer email@example.com",
            "article.body": "Full copyrighted article text",
        }
    )
    assert safe["feed.id"] == "bls-cpi"
    assert safe["feed.revision"] == 2
    assert safe["story.revision"] == 1
    assert safe["delivery.status"] == "new"
    assert "release.id" in safe
    assert "source.refs" not in safe  # Mixed invalid URL rejects entire source.refs list
    assert "OPENROUTER_API_KEY" not in safe
    assert "reviewer.notes" not in safe
    assert "article.body" not in safe

    # Valid source.refs passes
    valid_refs = sanitize_attributes({"source.refs": ("a" * 64, "b" * 64)})
    assert valid_refs["source.refs"] == ["a" * 64, "b" * 64]

    # Rejects secret-prefixed or URL-like item.id
    assert sanitize_attributes({"item.id": "https://example.com/a?token=1"}) == {}
    assert sanitize_attributes({"item.id": "sk-or-v1-secret"}) == {}

    db_path = _init_scout_db(tmp_path)
    repo = ScoutOperationalRepository(db_path, now=lambda: NOW)
    with pytest.raises(ValueError):
        repo.snapshot(since=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        repo.trace_run()

    # Exercise render_scout_dashboard cleanly with a lightweight Streamlit stub
    calls: list[str] = []

    class _Col:
        def metric(self, label, value):
            calls.append(f"metric:{label}={value}")

    class _StubSt:
        def title(self, text):
            calls.append(f"title:{text}")

        def caption(self, text):
            calls.append("caption")

        def error(self, text):
            calls.append(f"error:{text}")

        def warning(self, text):
            calls.append(f"warning:{text}")

        def info(self, text):
            calls.append("info")

        def subheader(self, text):
            calls.append(f"subheader:{text}")

        def columns(self, n):
            return [_Col() for _ in range(n)]

        def json(self, data):
            calls.append("json")

        def text_input(self, label):
            return ""

    import sys
    monkeypatch.setitem(sys.modules, "streamlit", _StubSt())
    render_scout_dashboard(repo)
    assert "title:News Scout Operational Monitoring" in calls
    assert any(c.startswith("subheader:Feed Freshness") for c in calls)


def test_incompatible_scout_deliveries_schema_does_not_break_other_projections(tmp_path):
    db_path = _init_scout_db(tmp_path)
    _seed_feed(db_path, "bls-cpi", "primary")
    _seed_cache(db_path, "bls-cpi", 1, NOW - timedelta(minutes=5))

    run_id = uuid4()
    inv_id = uuid4()
    with closing(open_database(db_path)) as conn:
        with conn:
            conn.execute("INSERT INTO artifacts VALUES (?, 'application/json', 10, '1.0')", ("a" * 64,))
            conn.execute(
                "INSERT INTO scout_run_requests(run_id, invocation_id, input_sha256) VALUES (?, ?, ?)",
                (str(run_id), str(inv_id), "a" * 64),
            )
            # Create a drifted/incompatible scout_deliveries schema missing expected columns
            conn.execute("DROP TABLE IF EXISTS scout_deliveries")
            conn.execute("CREATE TABLE scout_deliveries (delivery_id TEXT PRIMARY KEY, status TEXT NOT NULL)")

    repo = ScoutOperationalRepository(db_path, now=lambda: NOW)
    summary = repo.snapshot()
    assert summary.run_count == 1
    assert len(summary.feeds) == 1
    assert summary.feeds[0].feed_id == "bls-cpi"
    assert summary.handoffs.status == "unknown"
    assert summary.handoffs.availability_reason == "scout_deliveries_schema_incompatible"
    assert "scout_deliveries_schema_incompatible" in summary.missing_data
    assert "scout_database_unavailable" not in summary.missing_data

    corr = repo.trace_run(run_id=run_id, trace_id="1" * 32)
    assert corr.invocation_id == inv_id
    assert corr.input_sha256 == "a" * 64
    assert corr.handoffs == ()
    assert "scout_deliveries_schema_incompatible" in corr.missing_data
    assert "scout_database_unavailable" not in corr.missing_data


def UUID(val: str):
    from uuid import UUID as _UUID
    return _UUID(val)
