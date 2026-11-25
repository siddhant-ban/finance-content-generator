"""Unit tests for Scout durable delivery intent, retention enforcement, and replay contracts."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
from uuid import uuid4

import pytest

from app.contracts.evidence import SourceRecord
from app.contracts.scout import (
    AttributedStatement,
    ScoutFeedItem,
    ScoutMember,
    ScoutRunInput,
    ScoutTokenUsage,
    StoryCandidate,
    candidate_fingerprint,
    source_ref,
)
from app.scout.cli import format_audit_replay, format_reeval
from app.scout.delivery import (
    DeliveryRecord,
    ReplayInputSnapshot,
    record_replay_inputs,
    record_run_lineage,
)
from app.scout.replay import (
    ReplayCoverage,
    ReplayedCandidate,
    ScoutAuditReplayOutput,
    ScoutReevalOutput,
    UnreplayableItem,
    offline_network_disabled,
)
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database


def _make_request() -> ScoutRunInput:
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
            "cluster_version": "scout.cluster.v1",
            "prompt_version": "v1",
            "prompt_hash": "1" * 64,
            "provider": "fake",
            "model": "recorded-v1",
            "temperature": 0.0,
        },
        "release_id": str(uuid4()),
        "limits": {"max_items": 10, "max_tokens": 1000, "timeout_seconds": 15},
    }))


def test_migration_012_creates_delivery_and_append_only_replay_tables(tmp_path):
    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)

        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "scout_deliveries",
            "scout_run_lineage",
            "scout_run_outputs",
            "scout_replay_inputs",
            "scout_replay_responses",
        }.issubset(tables)

        cols = [row[1] for row in conn.execute("PRAGMA table_info(scout_deliveries)")]
        assert cols == [
            "story_id",
            "revision",
            "cluster_id",
            "artifact_sha256",
            "run_id",
            "invocation_id",
            "status",
            "replay_mode",
            "source_run_id",
            "trace_id",
            "error_code",
            "created_at",
            "updated_at",
        ]

        # Enforce CHECK constraint: metadata_only cannot store payload_json.
        req = _make_request()
        store = ArtifactStore(tmp_path / "artifacts", conn)
        # Put only DB row directly or use conn after disabling dir fsync? Here use conn directly.
        fake_sha = "a" * 64
        conn.execute(
            "INSERT INTO artifacts VALUES (?, 'application/json', 2, '1.0')",
            (fake_sha,),
        )
        conn.execute(
            "INSERT INTO scout_run_lineage("
            "run_id, invocation_id, replay_mode, source_run_id, source_input_sha256, "
            "input_sha256, config_hash, prompt_hash, provider, model, experiment_id, trace_id, created_at"
            ") VALUES (?, ?, 'live', NULL, NULL, ?, ?, ?, 'fake', 'recorded-v1', NULL, ?, ?)",
            (str(req.run_id), str(req.invocation_id), fake_sha, "b" * 64, "c" * 64, "0" * 32, "2025-09-01T00:00:00Z"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scout_replay_inputs("
                "run_id, item_order, item_id, rights, retained, retention_reason, "
                "source_id, source_revision, source_record_hash, canonical_url, "
                "payload_sha256, payload_json, created_at"
                ") VALUES (?, 0, 'item-1', 'metadata_only', 1, 'metadata_only', NULL, NULL, NULL, NULL, ?, '{}', ?)",
                (str(req.run_id), "d" * 64, "2025-09-01T00:00:00Z"),
            )
        conn.rollback()

        # Insert valid rows into each append-only table so BEFORE UPDATE / DELETE triggers fire.
        conn.execute(
            "INSERT INTO scout_replay_inputs("
            "run_id, item_order, item_id, rights, retained, retention_reason, "
            "source_id, source_revision, source_record_hash, canonical_url, "
            "payload_sha256, payload_json, created_at"
            ") VALUES (?, 0, 'item-meta', 'metadata_only', 0, 'metadata_only_rights', NULL, NULL, NULL, NULL, NULL, NULL, ?)",
            (str(req.run_id), "2025-09-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO scout_replay_responses("
            "run_id, story_id, revision, cluster_id, cluster_key, cluster_version, "
            "fingerprint, response_sha256, response_json, input_tokens, output_tokens, candidate_sha256, created_at"
            ") VALUES (?, ?, 1, ?, 'k1', 'v1', ?, ?, '{}', 1, 1, ?, ?)",
            (str(req.run_id), str(uuid4()), str(uuid4()), "e" * 64, "f" * 64, fake_sha, "2025-09-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO scout_run_outputs(run_id, output_sha256, outcome, created_at) "
            "VALUES (?, ?, 'empty', ?)",
            (str(req.run_id), fake_sha, "2025-09-01T00:00:00Z"),
        )
        conn.commit()

        # Append-only triggers forbid UPDATE/DELETE on lineage and replay tables.
        for table in ("scout_run_lineage", "scout_replay_inputs", "scout_replay_responses", "scout_run_outputs"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"DELETE FROM {table}")
            conn.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE scout_run_lineage SET model = 'tampered'")
        conn.rollback()


def test_replay_input_retention_policy_enforces_rights(tmp_path):
    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)
        req = _make_request()
        fake_sha = "a" * 64
        conn.execute("INSERT INTO artifacts VALUES (?, 'application/json', 2, '1.0')", (fake_sha,))
        conn.commit()
        record_run_lineage(
            conn,
            run_id=req.run_id,
            invocation_id=req.invocation_id,
            replay_mode="live",
            source_run_id=None,
            source_input_sha256=None,
            input_sha256=fake_sha,
            config_hash=req.configuration.record_hash(),
            prompt_hash=req.configuration.prompt_hash,
            provider=req.configuration.provider,
            model=req.configuration.model,
            experiment_id=None,
            trace_id="0" * 32,
        )

        pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
        permitted_item = ScoutFeedItem(
            headline="CPI rose 0.3 percent",
            source=SourceRecord(
                source_id=uuid4(),
                revision=1,
                original_url="https://www.bls.gov/cpi/1",
                canonical_url="https://www.bls.gov/cpi/1",
                publisher="BLS",
                source_class="primary",
                published_at=pub,
                publication_time_reason=None,
                retrieved_at=pub + timedelta(minutes=5),
                locator="Paragraph 1",
                excerpt="CPI-U rose 0.3 percent.",
                rights="excerpt_permitted",
                usage_notes="Public domain",
            ),
        )
        metadata_item = ScoutFeedItem(
            headline="Metadata only article",
            source=SourceRecord(
                source_id=uuid4(),
                revision=1,
                original_url="https://wire.example.com/1",
                canonical_url="https://wire.example.com/1",
                publisher="Wire",
                source_class="reputable_secondary",
                published_at=pub,
                publication_time_reason=None,
                retrieved_at=pub + timedelta(minutes=5),
                locator="Headline only",
                excerpt=None,
                rights="metadata_only",
                usage_notes="Metadata only",
            ),
        )

        snapshots = [
            ReplayInputSnapshot.from_feed_item(item_order=0, item_id="permitted", item=permitted_item),
            ReplayInputSnapshot.from_feed_item(item_order=1, item_id="meta", item=metadata_item),
            ReplayInputSnapshot.unretained(
                item_order=2,
                item_id="unk",
                rights="unknown",
                retention_reason="unknown_rights",
            ),
        ]
        record_replay_inputs(conn, run_id=req.run_id, items=snapshots)

        rows = conn.execute(
            "SELECT item_id, rights, retained, retention_reason, payload_json, payload_sha256 "
            "FROM scout_replay_inputs WHERE run_id = ? ORDER BY item_order",
            (str(req.run_id),),
        ).fetchall()
        assert len(rows) == 3
        assert rows[0][0] == "permitted" and rows[0][2] == 1 and rows[0][4] is not None and rows[0][5] is not None
        assert rows[1][0] == "meta" and rows[1][1] == "metadata_only" and rows[1][2] == 0 and rows[1][4] is None
        assert rows[2][0] == "unk" and rows[2][1] == "unknown" and rows[2][2] == 0 and rows[2][4] is None


def test_format_audit_replay_and_reeval_clearly_distinguish_modes():
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    source = SourceRecord(
        source_id=uuid4(),
        revision=1,
        original_url="https://www.bls.gov/cpi/1",
        canonical_url="https://www.bls.gov/cpi/1",
        publisher="BLS\nEvilControl",
        source_class="primary",
        published_at=pub,
        publication_time_reason=None,
        retrieved_at=pub + timedelta(minutes=5),
        locator="Paragraph 1",
        excerpt="CPI rose 0.3 percent.",
        rights="excerpt_permitted",
        usage_notes="Public domain",
    )
    member = ScoutFeedItem(headline="CPI release\x07", source=source)
    from uuid import uuid5
    sm = ScoutMember(headline=member.headline, source=source, ref=source_ref(source))
    cluster_id = uuid4()
    story_id = uuid5(cluster_id, "story")
    candidate = StoryCandidate(
        schema_version="1.0",
        schema_id="scout.candidate.v1",
        kind="editorial",
        story_id=story_id,
        cluster_id=cluster_id,
        revision=1,
        fingerprint=candidate_fingerprint((sm,)),
        representative=sm.ref,
        members=(sm,),
        summary=(AttributedStatement(text="CPI rose 0.3 percent.", sources=(sm.ref,)),),
        topics=("prices",),
        entities=("CPI",),
        warnings=("seasonally_adjusted",),
    )
    cand_sha = candidate.record_hash()
    coverage = ReplayCoverage(
        total_inputs=2,
        replayable_inputs=1,
        unreplayable_inputs=1,
        status="partial",
        unreplayable_items=(
            UnreplayableItem(item_id="meta-1", rights="metadata_only", reason="metadata_only_rights"),
        ),
    )
    replayed = ReplayedCandidate(
        story_id=story_id,
        cluster_id=cluster_id,
        revision=1,
        cluster_key="k1",
        fingerprint=candidate.fingerprint,
        original_artifact_sha256=cand_sha,
        reproduced_artifact_sha256=cand_sha,
        hash_matches=True,
        candidate=candidate,
        reported_tokens=ScoutTokenUsage(input_tokens=10, output_tokens=5),
    )
    audit = ScoutAuditReplayOutput(
        source_run_id=uuid4(),
        source_invocation_id=uuid4(),
        source_input_sha256="a" * 64,
        source_output_sha256="b" * 64,
        config_hash="c" * 64,
        prompt_hash="d" * 64,
        feed_snapshot="e" * 64,
        cluster_version="scout.cluster.v1",
        provider="fake",
        model="recorded-v1",
        replay_coverage=coverage,
        unreplayable_items=coverage.unreplayable_items,
        candidates=(replayed,),
        all_hashes_matched=True,
        reported_tokens=ScoutTokenUsage(input_tokens=10, output_tokens=5),
    )
    reeval = ScoutReevalOutput(
        run_id=uuid4(),
        invocation_id=uuid4(),
        source_run_id=audit.source_run_id,
        source_invocation_id=audit.source_invocation_id,
        source_input_sha256=audit.source_input_sha256,
        input_sha256="f" * 64,
        output_sha256="1" * 64,
        experiment_id="exp-reeval-1",
        hard_spend_cap_usd=Decimal("0.00"),
        source_config_hash=audit.config_hash,
        source_prompt_hash=audit.prompt_hash,
        source_model="recorded-v1",
        config_hash="2" * 64,
        prompt_hash="3" * 64,
        prompt_version="v2",
        provider="fake",
        model="recorded-v2",
        temperature=0.0,
        replay_coverage=coverage,
        unreplayable_items=coverage.unreplayable_items,
        candidates=(replayed,),
        reported_tokens=ScoutTokenUsage(input_tokens=12, output_tokens=6),
        trace_id="4" * 32,
    )

    audit_text = format_audit_replay(audit)
    reeval_text = format_reeval(reeval)
    assert "audit_replay" in audit_text
    assert "reeval" not in audit_text
    assert cand_sha in audit_text
    assert "metadata_only_rights" in audit_text
    assert "\x07" not in audit_text

    assert "reeval" in reeval_text
    assert "Network: disabled" in reeval_text
    assert "exp-reeval-1" in reeval_text
    assert "recorded-v2" in reeval_text
    assert str(audit.source_run_id) in reeval_text
    assert str(reeval.run_id) in reeval_text
    assert str(story_id) in reeval_text

    reeval_net = reeval.model_copy(update={"network_disabled": False})
    assert "Network: enabled" in format_reeval(reeval_net)


def test_offline_network_guard_blocks_socket_and_http_calls():
    import socket
    import urllib.request
    import httpx
    import requests

    with offline_network_disabled():
        with pytest.raises(RuntimeError, match="network disabled"):
            socket.create_connection(("127.0.0.1", 80), timeout=0.1)
        with pytest.raises(RuntimeError, match="network disabled"):
            urllib.request.urlopen("https://example.invalid")
        with pytest.raises(RuntimeError, match="network disabled"):
            httpx.get("https://example.invalid")
        with pytest.raises(RuntimeError, match="network disabled"):
            requests.get("https://example.invalid")
