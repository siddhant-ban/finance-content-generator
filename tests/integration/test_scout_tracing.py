"""Integration tests for linked Scout tracing, operational projection, outage recovery, and sanitization."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
import pytest
import requests

from app.contracts.scout import (
    SCOUT_PROJECT,
    ScoutConfiguration,
    ScoutFingerprintSnapshot,
    ScoutLimits,
    ScoutRunInput,
    ScoutTopicPolicy,
)
from app.monitoring.scout_ops import ScoutOperationalRepository
from app.monitoring.scout_ui import build_dashboard_view
from app.observability.tracing import Telemetry
from app.prompts import load_prompt
from app.scout.eval_config import load_scout_eval_config
from app.scout.ledger import SpendLedger
from app.scout.openrouter import OpenRouterScoutEnricher, SCOUT_ENRICHMENT_PROMPT_MANIFEST
from app.scout.registry import FeedRegistry, FeedSettings
from app.scout.rss import RSSPoller
from app.scout.run import run_scout
from app.storage import artifacts as artifacts_module
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database

if os.name == "nt":
    @pytest.fixture(autouse=True)
    def _windows_artifact_dir_sync_compat(monkeypatch):
        """Windows cannot open directories with os.O_RDONLY for POSIX directory fsync."""
        real_open = os.open
        real_fsync = os.fsync
        real_close = os.close
        skip_fds: set[int] = set()
        next_fd = 900_000

        def compat_open(path, flags, *args, **kwargs):
            if os.path.isdir(path) and flags == os.O_RDONLY:
                nonlocal next_fd
                next_fd += 1
                skip_fds.add(next_fd)
                return next_fd
            return real_open(path, flags, *args, **kwargs)

        def compat_fsync(fd):
            if fd in skip_fds:
                return None
            return real_fsync(fd)

        def compat_close(fd):
            if fd in skip_fds:
                skip_fds.discard(fd)
                return None
            return real_close(fd)

        class _CompatOS:
            O_RDONLY = os.O_RDONLY
            walk = staticmethod(os.walk)
            fdopen = staticmethod(os.fdopen)
            replace = staticmethod(os.replace)
            open = staticmethod(compat_open)
            fsync = staticmethod(compat_fsync)
            close = staticmethod(compat_close)

        monkeypatch.setattr(artifacts_module, "os", _CompatOS())


SECRET_KEY = "sk-or-v1-secret-openrouter-test-key"
BEARER_SENTINEL = "BEARER_SECRET_TOKEN_999"
REVIEWER_SENTINEL = "private-reviewer-notes@example.com"
BODY_SENTINEL = "FULL_COPYRIGHTED_ARTICLE_BODY_SENTINEL"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _rss_payload() -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>BLS Releases</title>
    <item>
      <title>Consumer Price Index Summary</title>
      <link>https://www.bls.gov/news.release/cpi_fresh.htm?utm_source=feed</link>
      <pubDate>Wed, 25 Nov 2026 11:00:00 GMT</pubDate>
      <description>{BODY_SENTINEL}</description>
    </item>
    <item>
      <title>Old Price Index Summary</title>
      <link>https://www.bls.gov/news.release/cpi_stale.htm</link>
      <pubDate>Mon, 23 Nov 2026 11:00:00 GMT</pubDate>
      <description>{BODY_SENTINEL}</description>
    </item>
    <item>
      <title>Quarantined Foreign Ref Summary</title>
      <link>https://www.bls.gov/news.release/cpi_quarantine.htm</link>
      <pubDate>Wed, 25 Nov 2026 11:30:00 GMT</pubDate>
      <description>{BODY_SENTINEL}</description>
    </item>
    <item>
      <title></title>
      <link>https://www.bls.gov/news.release/cpi_bad.htm</link>
      <pubDate>Wed, 25 Nov 2026 11:45:00 GMT</pubDate>
    </item>
  </channel>
</rss>""".encode("utf-8")


def _setup_run(tmp_path, monkeypatch, *, collector_endpoint: str | None = None):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET_KEY)
    monkeypatch.delenv("FINANCE_ALLOW_PAID_PROVIDERS", raising=False)
    if collector_endpoint is not None:
        monkeypatch.setenv("FINANCE_PHOENIX_ENDPOINT", collector_endpoint)

    db_path = tmp_path / "scout.sqlite3"
    tel_path = tmp_path / "telemetry.sqlite3"
    conn = open_database(db_path)
    migrate(conn)
    store = ArtifactStore(tmp_path / "artifacts", conn)
    registry = FeedRegistry(conn)
    registry.add(
        "bls-cpi",
        FeedSettings(
            endpoint="https://feeds.example.test/bls-cpi.rss",
            publisher="U.S. Bureau of Labor Statistics",
            source_class="primary",
            rights="metadata_only",
            usage_notes="Metadata only",
        ),
        actor="curator",
        reason="seed",
    )
    registry.add(
        "slow-fed",
        FeedSettings(
            endpoint="https://feeds.example.test/slow-fed.xml",
            publisher="Federal Reserve",
            source_class="primary",
            rights="metadata_only",
            usage_notes="Metadata only",
        ),
        actor="curator",
        reason="seed",
    )
    snapshot = registry.snapshot()

    prompt_dir = tmp_path / "prompts"
    manifest_dir = prompt_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    raw_manifest = json.loads(SCOUT_ENRICHMENT_PROMPT_MANIFEST.read_bytes())
    raw_prompt_bytes = (SCOUT_ENRICHMENT_PROMPT_MANIFEST.parent / raw_manifest["path"]).read_bytes().replace(b"\r\n", b"\n")
    lf_prompt_path = prompt_dir / "scout_enrichment.v1.md"
    lf_prompt_path.write_bytes(raw_prompt_bytes)
    lf_manifest_path = manifest_dir / "scout_enrichment.v1.json"
    lf_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "prompt_id": "scout_enrichment",
                "prompt_version": "scout_enrichment.v1",
                "path": "../scout_enrichment.v1.md",
                "sha256": hashlib.sha256(raw_prompt_bytes).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    prompt = load_prompt(lf_manifest_path)
    eval_config = load_scout_eval_config(profile_name="ling")
    ledger = SpendLedger(db_path)

    def feed_handler(request: httpx.Request) -> httpx.Response:
        if "slow-fed" in str(request.url):
            raise httpx.ReadTimeout("slow feed timed out")
        return httpx.Response(200, text=_rss_payload().decode("utf-8"), headers={"Content-Type": "application/rss+xml; charset=utf-8"})

    def llm_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": eval_config.model_id,
                        "endpoints": [
                            {
                                "name": "test-endpoint",
                                "status": 0,
                                "supported_parameters": ["structured_outputs", "max_tokens", "temperature"],
                                "pricing": {"prompt": "0", "completion": "0"},
                            }
                        ],
                    }
                },
            )
        body = json.loads(request.content)
        user_payload = json.loads(body["messages"][1]["content"])
        member = user_payload["members"][0]
        ref = dict(member["ref"])
        if "quarantine" in member["source"]["canonical_url"]:
            # Fabricated foreign source_id forces quarantine (foreign_reference).
            ref["source_id"] = str(uuid4())
        enrichment = {
            "schema_version": "1.0",
            "summary": [{"text": "Consumer prices rose modestly.", "sources": [ref]}],
            "topics": ["prices"],
            "entities": ["Consumer Price Index"],
            "warnings": [],
            "uncertainty": [],
        }
        return httpx.Response(
            200,
            json={
                "model": "inclusionai/ling-3.2-flash:free",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(enrichment)}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": "0"},
            },
        )

    poller = RSSPoller(
        registry,
        snapshot,
        httpx.Client(transport=httpx.MockTransport(feed_handler)),
        now=lambda: NOW,
    )
    enricher = OpenRouterScoutEnricher(
        eval_config=eval_config,
        prompt=prompt,
        ledger=ledger,
        experiment_id="exp-trace",
        prompt_manifest_path=lf_manifest_path,
        transport=httpx.MockTransport(llm_handler),
    )
    request = ScoutRunInput(
        schema_version="1.0",
        run_id=uuid4(),
        invocation_id=uuid4(),
        window_start=NOW - timedelta(hours=12),
        window_end=NOW + timedelta(hours=1),
        topic_policy=ScoutTopicPolicy(version="v1", topics=("prices", "jobs", "borrowing")),
        fingerprints=ScoutFingerprintSnapshot(version="v1", known=()),
        configuration=ScoutConfiguration(
            version="v1.0",
            feed_snapshot=snapshot.record_hash(),
            cluster_version="v1",
            prompt_version=prompt.prompt_version,
            prompt_hash=prompt.sha256,
            provider="openrouter",
            model=eval_config.model_id,
            temperature=0.0,
        ),
        release_id=uuid4(),
        limits=ScoutLimits(max_items=20, max_tokens=5000, timeout_seconds=30.0),
    )
    telemetry = Telemetry(tel_path)
    return db_path, tel_path, conn, store, telemetry, request, poller, enricher


def test_linked_scout_trace_hierarchy_and_quarantined_item_projection(tmp_path, monkeypatch):
    db_path, tel_path, conn, store, telemetry, request, poller, enricher = _setup_run(tmp_path, monkeypatch)
    try:
        output = run_scout(
            request,
            store=store,
            telemetry=telemetry,
            fetch=poller,
            enrich=enricher,
        )
        pending = telemetry.pending_records()
    finally:
        telemetry.shutdown(0)
        conn.close()

    assert output.outcome == "partial"
    assert len(output.candidates) == 1

    run_spans = [s for s in pending if s.trace_id == output.trace_id]
    names = {s.name for s in run_spans}
    assert {"agent.invoke", "scout.fetch", "scout.item", "model.call", "workflow.transition", "scout.dispatch"} <= names
    assert all(s.project == SCOUT_PROJECT and s.run_id == request.run_id for s in run_spans)

    root = next(s for s in run_spans if s.name == "agent.invoke")
    assert root.parent_span_id is None
    span_ids = {s.span_id for s in run_spans}
    assert all(s.parent_span_id in span_ids for s in run_spans if s.span_id != root.span_id)
    assert root.attributes["release.id"] == str(request.release_id)
    assert root.attributes["prompt.version"] == request.configuration.prompt_version
    assert root.attributes["prompt.hash"] == request.configuration.prompt_hash
    assert root.attributes["model.provider"] == "openrouter"
    assert root.attributes["tool.ref"] == request.configuration.feed_snapshot
    assert root.attributes["run.outcome"] == "partial"
    assert root.attributes["result.status"] == "BLOCKED"
    assert root.attributes["latency_ms"] >= 0

    tool_call = next(s for s in run_spans if s.name == "tool.call")
    fetch_spans = {s.attributes["feed.id"]: s for s in run_spans if s.name == "scout.fetch"}
    assert set(fetch_spans) == {"bls-cpi", "slow-fed"}
    assert all(s.parent_span_id == tool_call.span_id for s in fetch_spans.values())
    assert fetch_spans["bls-cpi"].attributes["feed.status"] == "read"
    assert fetch_spans["bls-cpi"].attributes["feed.revision"] == 1
    assert fetch_spans["bls-cpi"].attributes["source.count"] == 4
    assert fetch_spans["slow-fed"].attributes["feed.status"] == "unavailable"
    assert fetch_spans["slow-fed"].attributes["feed.reason"] == "timeout"

    item_spans = [s for s in run_spans if s.name == "scout.item"]
    assert len(item_spans) == 4
    reasons = {s.attributes["item.reason"]: s for s in item_spans}
    assert reasons["candidate_created"].attributes["item.status"] == "new"
    assert reasons["candidate_created"].attributes["source.class"] == "primary"
    assert reasons["candidate_created"].attributes["story.revision"] == 1
    assert reasons["outside_window"].attributes["item.status"] == "withheld"
    assert reasons["malformed_feed_item"].attributes["item.status"] == "failed"
    assert reasons["invalid_enrichment"].attributes["item.status"] == "failed"

    model_spans = [s for s in run_spans if s.name == "model.call"]
    assert len(model_spans) == 2
    failed_models = [s for s in model_spans if "error.code" in s.attributes]
    assert len(failed_models) == 1
    assert failed_models[0].status == "ERROR"
    assert any(s.attributes.get("usage.input_tokens") == 120 for s in model_spans)

    transition_span = next(s for s in run_spans if s.name == "workflow.transition")
    dispatch_spans = [s for s in run_spans if s.name == "scout.dispatch"]
    assert len(dispatch_spans) == 1
    assert dispatch_spans[0].parent_span_id == transition_span.span_id
    assert dispatch_spans[0].attributes["delivery.status"] == "new"
    assert dispatch_spans[0].attributes["story.revision"] == 1
    assert len(dispatch_spans[0].attributes["artifact.hash"]) == 64

    # Check Operational Repository correlation & projection
    repo = ScoutOperationalRepository(db_path, telemetry_path=tel_path, now=lambda: NOW)
    summary = repo.snapshot()
    assert summary.item_rates.total_items == 4
    assert summary.item_rates.stale_count == 1
    assert summary.item_rates.invalid_count == 2
    assert summary.quarantine.quarantine_count >= 1
    assert "foreign_reference" in summary.quarantine.reasons
    assert summary.budget.status == "measured"
    assert summary.budget.reconciled_count == 2

    corr = repo.trace_run(run_id=request.run_id)
    assert corr.trace_id == output.trace_id
    assert corr.run_id == request.run_id
    assert len(corr.spans) == len(run_spans)
    assert len(corr.story_revisions) == 1


def test_phoenix_outage_buffering_correlation_and_recovery_plus_sanitization(tmp_path, monkeypatch):
    endpoint = "http://127.0.0.1:16006/v1/traces"
    db_path, tel_path, conn, store, telemetry, request, poller, enricher = _setup_run(
        tmp_path, monkeypatch, collector_endpoint=endpoint
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"authorization=Bearer {BEARER_SENTINEL}")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"reviewer.notes={REVIEWER_SENTINEL}")

    collector_up = {"up": False}
    exported_bodies: list[bytes] = []

    def fake_send(adapter, req, **kwargs):
        resp = requests.Response()
        resp.request = req
        if not collector_up["up"]:
            resp.status_code = 503
            resp._content = b"collector unavailable"
            return resp
        exported_bodies.append(req.body or b"")
        resp.status_code = 200
        resp._content = ExportTraceServiceResponse().SerializeToString()
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)

    try:
        output = run_scout(
            request,
            store=store,
            telemetry=telemetry,
            fetch=poller,
            enrich=enricher,
        )
        outage_flush = telemetry.flush(timeout_seconds=2.0)
        assert outage_flush.error_code == "export_failed"
        assert outage_flush.pending_count and outage_flush.pending_count > 0
        pending_count = outage_flush.pending_count

        repo = ScoutOperationalRepository(db_path, telemetry_path=tel_path, now=lambda: NOW)
        outage_summary = repo.snapshot()
        assert outage_summary.telemetry.phoenix_outage is True
        assert outage_summary.telemetry.pending_spans == pending_count
        assert outage_summary.telemetry.delivered_spans == 0
        assert any(alert.code == "phoenix_outage" for alert in outage_summary.alerts)

        corr = repo.trace_run(trace_id=output.trace_id)
        assert corr.run_id == request.run_id
        assert corr.telemetry_delivery_status == "outage_buffered"
        assert len(corr.spans) == pending_count

        view = build_dashboard_view(outage_summary, corr)
        serialized_all = json.dumps(view) + outage_summary.model_dump_json() + corr.model_dump_json()
        for secret in (SECRET_KEY, BEARER_SENTINEL, REVIEWER_SENTINEL, BODY_SENTINEL):
            assert secret not in serialized_all

        # Simulate Phoenix collector recovery and flush
        collector_up["up"] = True
        recovery_flush = telemetry.flush(timeout_seconds=2.0)
        assert recovery_flush.exported == pending_count
        assert recovery_flush.pending_count == 0
        assert recovery_flush.error_code is None

        recovered_summary = repo.snapshot()
        assert recovered_summary.telemetry.phoenix_outage is False
        assert recovered_summary.telemetry.pending_spans == 0
        assert recovered_summary.telemetry.delivered_spans == pending_count
        assert not any(alert.code == "phoenix_outage" for alert in recovered_summary.alerts)

        # Verify zero secrets/private data in raw sqlite and exported protobuf bytes
        raw_tel_bytes = tel_path.read_bytes() + b"".join(exported_bodies)
        for secret in (SECRET_KEY, BEARER_SENTINEL, REVIEWER_SENTINEL, BODY_SENTINEL):
            assert secret.encode("utf-8") not in raw_tel_bytes
    finally:
        telemetry.shutdown(0)
        conn.close()
