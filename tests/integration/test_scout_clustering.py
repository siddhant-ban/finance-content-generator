"""Integration tests for clustering, conflicts, corrections, and history inspection."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import itertools
import json
import os
import sqlite3
from uuid import uuid4

import pytest

from app.contracts.evidence import SourceRecord
from app.contracts.scout import ScoutEnrichment, ScoutFeedItem, ScoutRunInput, ScoutTokenUsage, StoryCandidate
from app.contracts.workflow import StoryState
from app.observability.tracing import Telemetry
from app.scout.cli import format_history, format_result, main
from app.scout.cluster import CLUSTER_VERSION
from app.scout.run import ModelResult, run_scout
from app.scout.workflow import scout_state, story_revisions
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database
from app.workflow.state import TransitionRequest


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
    window_start: str = "2025-09-01T00:00:00Z",
    window_end: str = "2025-12-01T00:00:00Z",
    known: tuple[str, ...] = (),
) -> ScoutRunInput:
    return ScoutRunInput.model_validate_json(json.dumps({
        "schema_version": "1.0",
        "run_id": str(uuid4()),
        "invocation_id": str(uuid4()),
        "window_start": window_start,
        "window_end": window_end,
        "topic_policy": {"version": "v1", "topics": ["prices", "jobs", "borrowing"]},
        "fingerprints": {"version": "v1", "known": list(known)},
        "configuration": {
            "version": "v1",
            "feed_snapshot": "0" * 64,
            "cluster_version": CLUSTER_VERSION,
            "prompt_version": "v1",
            "prompt_hash": "1" * 64,
            "provider": "fake",
            "model": "recorded-v1",
            "temperature": 0.0,
        },
        "release_id": str(uuid4()),
        "limits": {"max_items": 20, "max_tokens": 2000, "timeout_seconds": 15},
    }))


def _feed_item(
    *,
    headline: str,
    canonical_url: str,
    publisher: str,
    source_class: str,
    published_at: datetime | None,
    excerpt: str | None = None,
    locator: str = "Opening paragraph",
    revision: int = 1,
    source_id=None,
) -> ScoutFeedItem:
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
            publication_time_reason=None if published_at else "Missing publication date in feed",
            retrieved_at=retrieved,
            locator=locator,
            excerpt=excerpt,
            rights="excerpt_permitted" if excerpt else "metadata_only",
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


def test_public_run_clusters_overlapping_accounts_separates_periods_and_withholds_undated(env):
    _, _, store, telemetry = env
    sep_pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    oct_pub = datetime(2025, 11, 13, 13, 30, tzinfo=timezone.utc)

    batch = {
        "bls-sep": _feed_item(
            headline="Consumer Price Index - September 2025",
            canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
            publisher="U.S. Bureau of Labor Statistics",
            source_class="primary",
            published_at=sep_pub,
            excerpt="The all-items CPI-U rose 0.3 percent in September 2025.",
        ).canonical_bytes(),
        "reuters-sep": _feed_item(
            headline="U.S. consumer prices rose 0.3% in September 2025",
            canonical_url="https://reuters.example.com/markets/us-cpi-sep-2025",
            publisher="Reuters",
            source_class="reputable_secondary",
            published_at=sep_pub + timedelta(minutes=25),
            excerpt="Consumer prices increased 0.3 percent in September 2025.",
        ).canonical_bytes(),
        "bls-oct": _feed_item(
            headline="Consumer Price Index - October 2025",
            canonical_url="https://www.bls.gov/news.release/archives/cpi_11132025.htm",
            publisher="U.S. Bureau of Labor Statistics",
            source_class="primary",
            published_at=oct_pub,
            excerpt="The all-items CPI-U rose 0.2 percent in October 2025.",
        ).canonical_bytes(),
        "undated-item": _feed_item(
            headline="CPI commentary without timestamp",
            canonical_url="https://blog.example.com/undated-cpi",
            publisher="Example Commentary",
            source_class="commentary",
            published_at=None,
            locator="Lead paragraph",
        ).canonical_bytes(),
    }

    output = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: batch,
        enrich=_enrich,
    )

    assert output.outcome == "complete"
    assert len(output.candidates) == 2
    by_id = {item.item_id: item for item in output.items}
    assert by_id["undated-item"].status == "withheld"
    assert by_id["undated-item"].reason == "no_publication_time"
    assert by_id["undated-item"].candidate_ref is None
    assert by_id["bls-sep"].candidate_ref == by_id["reuters-sep"].candidate_ref
    assert by_id["bls-sep"].candidate_ref != by_id["bls-oct"].candidate_ref

    # Typed Novelty fixture deserializes both candidates directly.
    novelty_candidates = [StoryCandidate.model_validate_json(store.read(ref)) for ref in output.candidates]
    assert len({c.cluster_id for c in novelty_candidates}) == 2
    assert len({c.story_id for c in novelty_candidates}) == 2
    sep_candidate = next(c for c in novelty_candidates if len(c.members) == 2)
    assert {m.source.publisher for m in sep_candidate.members} == {
        "U.S. Bureau of Labor Statistics",
        "Reuters",
    }
    assert all(
        (m.ref.source_id, m.ref.source_revision, m.ref.source_record_hash)
        == (m.source.source_id, m.source.revision, m.source.record_hash())
        for m in sep_candidate.members
    )
    rendered = format_result(output, store)
    assert "Reuters" in rendered
    assert "no_publication_time" in rendered


def test_dated_disagreement_keeps_all_members_and_sets_conflicting_sources_warning(env):
    _, _, store, telemetry = env
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    batch = {
        "primary": _feed_item(
            headline="Consumer Price Index - September 2025",
            canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
            publisher="U.S. Bureau of Labor Statistics",
            source_class="primary",
            published_at=pub,
            excerpt="The all-items CPI-U rose 0.4 percent in September 2025.",
        ).canonical_bytes(),
        "secondary": _feed_item(
            headline="U.S. consumer prices rose 0.2% in September 2025",
            canonical_url="https://wire.example.com/cpi-sep-2025",
            publisher="Wire News",
            source_class="reputable_secondary",
            published_at=pub + timedelta(minutes=15),
            excerpt="Consumer prices increased 0.2 percent in September 2025.",
        ).canonical_bytes(),
    }

    output = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: batch,
        enrich=_enrich,
    )

    assert len(output.candidates) == 1
    candidate = StoryCandidate.model_validate_json(store.read(output.candidates[0]))
    assert len(candidate.members) == 2
    assert "conflicting_sources" in candidate.warnings
    assert len(candidate.summary) == 2
    assert {s.sources[0] for s in candidate.summary} == {m.ref for m in candidate.members}


def test_publisher_correction_preserves_rev1_records_history_and_reworks_state(env, capsys):
    root, conn, store, telemetry = env
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    bls_source_id = uuid4()

    item_v1 = _feed_item(
        source_id=bls_source_id,
        revision=1,
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        publisher="U.S. Bureau of Labor Statistics",
        source_class="primary",
        published_at=pub,
        excerpt="The all-items CPI-U rose 0.4 percent in September 2025.",
    )
    run1 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v1.canonical_bytes()},
        enrich=_enrich,
    )
    assert [item.status for item in run1.items] == ["new"]
    (ref_v1,) = run1.candidates
    cand_v1 = StoryCandidate.model_validate_json(store.read(ref_v1))
    assert cand_v1.revision == 1
    v1_bytes = store.read(ref_v1)

    # Feed a publisher correction: revision=2, updated headline & excerpt.
    item_v2 = _feed_item(
        source_id=bls_source_id,
        revision=2,
        headline="Correction: Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        publisher="U.S. Bureau of Labor Statistics",
        source_class="primary",
        published_at=pub,
        excerpt="Corrected: The all-items CPI-U rose 0.3 percent in September 2025.",
    )
    run2 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v2.canonical_bytes()},
        enrich=_enrich,
    )
    assert [(item.status, item.reason) for item in run2.items] == [("updated", "candidate_created")]
    (ref_v2,) = run2.candidates
    assert ref_v2 != ref_v1
    cand_v2 = StoryCandidate.model_validate_json(store.read(ref_v2))
    assert cand_v2.story_id == cand_v1.story_id
    assert cand_v2.cluster_id == cand_v1.cluster_id
    assert cand_v2.revision == 2
    assert store.read(ref_v1) == v1_bytes  # Prior artifact remains intact and unmutated.

    # StateService rework trail: DISCOVERED -> CLUSTERED -> NEEDS_REVISION -> CLUSTERED.
    state = scout_state(store)
    events = state.events(cand_v1.story_id)
    assert [(e.to_state, e.reason_code) for e in events] == [
        (StoryState.DISCOVERED, "candidate_created"),
        (StoryState.CLUSTERED, "stage_complete"),
        (StoryState.NEEDS_REVISION, "source_correction"),
        (StoryState.CLUSTERED, "source_correction"),
    ]
    snapshot = state.get(cand_v1.story_id)
    assert snapshot.artifacts["candidate"] == ref_v1
    assert snapshot.artifacts["cluster"] == ref_v2

    # Revision history persisted in 010_scout_clusters.sql (immutable).
    revisions = story_revisions(store, cand_v1.story_id)
    assert [r.revision for r in revisions] == [1, 2]
    assert [r.artifact_ref for r in revisions] == [ref_v1, ref_v2]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE scout_story_revisions SET reason_code = 'tampered'")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM scout_story_revisions")
    conn.rollback()

    # Replay of correction is idempotent and does not create revision 3.
    replay = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v2.canonical_bytes()},
        enrich=_enrich,
    )
    assert [(item.status, item.reason) for item in replay.items] == [("unchanged", "already_delivered")]
    assert len(story_revisions(store, cand_v1.story_id)) == 2

    # Editor can inspect BOTH revision 1 and revision 2 via format_result, format_history, and CLI.
    report_view = format_result(run2, store)
    assert "Revision 1" in report_view
    assert "Revision 2" in report_view
    assert "0.4 percent" in report_view
    assert "0.3 percent" in report_view

    history_view = format_history(store, cand_v1.story_id)
    assert "Revision 1" in history_view
    assert "Revision 2" in history_view
    assert cand_v1.fingerprint in history_view
    assert cand_v2.fingerprint in history_view

    capsys.readouterr()
    assert main(["history", "--output", str(root), "--story-id", str(cand_v1.story_id)]) == 0
    cli_history = capsys.readouterr().out
    assert "Revision 1" in cli_history
    assert "Revision 2" in cli_history


def test_correction_never_bypasses_state_guards_when_story_not_clustered(env):
    _, _, store, telemetry = env
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    bls_source_id = uuid4()
    item_v1 = _feed_item(
        source_id=bls_source_id,
        revision=1,
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        publisher="U.S. Bureau of Labor Statistics",
        source_class="primary",
        published_at=pub,
        excerpt="The all-items CPI-U rose 0.4 percent in September 2025.",
    )
    run1 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v1.canonical_bytes()},
        enrich=_enrich,
    )
    cand_v1 = StoryCandidate.model_validate_json(store.read(run1.candidates[0]))
    state = scout_state(store)
    snap = state.get(cand_v1.story_id)
    state.transition(TransitionRequest(
        story_id=cand_v1.story_id,
        target=StoryState.REJECTED,
        expected_version=snap.version,
        caller_scope="test",
        idempotency_key="reject-story",
        reason_code="editorial_reject",
    ))

    item_v2 = _feed_item(
        source_id=bls_source_id,
        revision=2,
        headline="Correction: Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        publisher="U.S. Bureau of Labor Statistics",
        source_class="primary",
        published_at=pub,
        excerpt="Corrected: The all-items CPI-U rose 0.3 percent in September 2025.",
    )
    run2 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v2.canonical_bytes()},
        enrich=_enrich,
    )
    assert run2.outcome == "failed"
    assert [(item.status, item.reason) for item in run2.items] == [("failed", "revision_conflict")]
    assert len(story_revisions(store, cand_v1.story_id)) == 1


def test_changed_fingerprint_same_source_revision_updates_and_downgrade_fails(env):
    _, _, store, telemetry = env
    pub = datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc)
    bls_source_id = uuid4()

    item_v1 = _feed_item(
        source_id=bls_source_id,
        revision=1,
        headline="Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        publisher="U.S. Bureau of Labor Statistics",
        source_class="primary",
        published_at=pub,
        excerpt="The all-items CPI-U rose 0.4 percent in September 2025.",
    )
    run1 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v1.canonical_bytes()},
        enrich=_enrich,
    )
    cand_v1 = StoryCandidate.model_validate_json(store.read(run1.candidates[0]))

    # Changed fingerprint (new secondary account joined cluster) at same source_revision=1.
    secondary = _feed_item(
        revision=1,
        headline="U.S. consumer prices rose 0.4% in September 2025",
        canonical_url="https://wire.example.com/cpi-sep-2025",
        publisher="Wire News",
        source_class="reputable_secondary",
        published_at=pub + timedelta(minutes=20),
        excerpt="Consumer prices increased 0.4 percent in September 2025.",
    )
    run2 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {
            "bls-sep": item_v1.canonical_bytes(),
            "wire-sep": secondary.canonical_bytes(),
        },
        enrich=_enrich,
    )
    assert [(item.status, item.reason) for item in run2.items] == [
        ("updated", "candidate_created"),
        ("updated", "candidate_created"),
    ]
    (ref_v2,) = run2.candidates
    cand_v2 = StoryCandidate.model_validate_json(store.read(ref_v2))
    assert cand_v2.story_id == cand_v1.story_id
    assert cand_v2.revision == 2
    assert len(cand_v2.members) == 2

    # Advance primary source to revision=2, then feed stale revision=1 -> revision_conflict.
    item_v2 = _feed_item(
        source_id=bls_source_id,
        revision=2,
        headline="Correction: Consumer Price Index - September 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
        publisher="U.S. Bureau of Labor Statistics",
        source_class="primary",
        published_at=pub,
        excerpt="Corrected: The all-items CPI-U rose 0.3 percent in September 2025.",
    )
    run3 = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v2.canonical_bytes()},
        enrich=_enrich,
    )
    assert StoryCandidate.model_validate_json(store.read(run3.candidates[0])).revision == 3

    stale = run_scout(
        _request(),
        store=store,
        telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: {"bls-sep": item_v1.canonical_bytes()},
        enrich=_enrich,
    )
    assert stale.outcome == "failed"
    assert [(item.status, item.reason) for item in stale.items] == [("failed", "revision_conflict")]
    assert len(story_revisions(store, cand_v1.story_id)) == 3

