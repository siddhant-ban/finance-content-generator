"""Exercise Scout's public transport and retained workflow boundaries."""
from contextlib import closing
import json
from pathlib import Path
from uuid import uuid4, uuid5

import pytest
from langchain_core.tracers.context import tracing_v2_callback_var
from langsmith.utils import tracing_is_enabled
from pydantic import ValidationError

from app.contracts.scout import ScoutEnrichment, ScoutFeedItem, ScoutRunOutput, ScoutTokenUsage, StoryCandidate
from app.contracts.workflow import StoryState
from app.demo import review_policy
from app.observability.tracing import Telemetry
from app.scout.cli import ScoutRecording
from app.scout.run import ModelResult, run_scout
from app.scout.workflow import scout_state
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database
from app.workflow.state import PolicyDenied, TransitionRequest
from test_scout import RECORDING, run_request


@pytest.fixture
def boundary(tmp_path):
    telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)
        yield ArtifactStore(tmp_path / "artifacts", conn), telemetry
    telemetry.shutdown(timeout_seconds=0)


def fixture_item():
    recording = ScoutRecording.model_validate_json(RECORDING.read_bytes())
    return ScoutFeedItem.model_validate_json(json.dumps(next(iter(recording.feed.items.values()))))


def another_item(item):
    payload = item.model_dump(mode="json")
    payload["headline"] = "Synthetic transport exercise"
    payload["source"].update(
        source_id=str(uuid4()), publisher="Synthetic transport exercise",
        original_url="https://example.invalid/synthetic-scout",
        canonical_url="https://example.invalid/synthetic-scout",
        locator="Synthetic fixture", excerpt=None, rights="metadata_only",
        usage_notes="Original synthetic transport test with no news claim.",
    )
    return ScoutFeedItem.model_validate_json(json.dumps(payload))


def response(members):
    enrichment = ScoutEnrichment.model_validate_json(json.dumps({
        "schema_version": "1.0",
        "summary": [{"text": "The source supplied a test observation.", "sources": [
            member.ref.model_dump(mode="json") for member in members
        ]}],
        "topics": ["prices"], "entities": [], "warnings": [],
    }))
    return ModelResult(enrichment.canonical_bytes(), ScoutTokenUsage(input_tokens=3, output_tokens=2))


def execute(boundary, batch, *, enrich=None, request=None, **kwargs):
    store, telemetry = boundary
    return run_scout(
        request or run_request(), store=store, telemetry=telemetry,
        fetch=lambda *_args, **_kwargs: batch,
        enrich=enrich or (lambda _request, members, **_kwargs: response(members)),
        **kwargs,
    )


def test_empty_batch_and_multiple_distinct_candidates_round_trip(boundary):
    store, telemetry = boundary
    empty = execute(boundary, {})
    assert empty.outcome == "empty"
    assert empty.items == empty.candidates == ()
    item = fixture_item()
    second = another_item(item)
    output = execute(boundary, {"bls": item.canonical_bytes(), "second": second.canonical_bytes()})
    assert output.outcome == "complete"
    assert [item.status for item in output.items] == ["new", "new"]
    assert ScoutRunOutput.model_validate_json(output.canonical_bytes()) == output
    candidates = [StoryCandidate.model_validate_json(store.read(ref)) for ref in output.candidates]
    assert len({candidate.story_id for candidate in candidates}) == 2
    assert len({candidate.cluster_id for candidate in candidates}) == 2
    traces = [span for _, span in telemetry.outbox.pending() if span.trace_id == output.trace_id]
    assert {span.name for span in traces} >= {"agent.invoke", "tool.call", "model.call", "workflow.transition"}
    assert all(span.project == "finance-media-news-scout" and span.run_id == output.run_id for span in traces)
    assert all(b"Consumer Price Index" not in span.canonical_bytes() for span in traces)


def test_unprocessed_items_are_reported_for_item_and_token_limits(boundary):
    item = fixture_item()
    second = another_item(item)
    batch = {"first": item.canonical_bytes(), "second": second.canonical_bytes(), "third": b"unparsed"}
    request = run_request(limits={"max_items": 2, "max_tokens": 5, "timeout_seconds": 30})
    calls = []

    def enrich(_request, members, *, max_tokens, timeout_seconds):
        calls.append((max_tokens, timeout_seconds))
        return response(members)

    result = execute(boundary, batch, request=request, enrich=enrich)
    assert result.outcome == "partial"
    assert [(item.status, item.reason) for item in result.items] == [
        ("new", "candidate_created"), ("unprocessed", "token_limit"), ("unprocessed", "item_limit"),
    ]
    assert len(calls) == 1
    assert calls[0][0] == result.reported_tokens.total == 5
    assert 0 < calls[0][1] <= 30


def test_expired_provider_output_is_never_delivered(boundary, monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    now = 0.0
    request = run_request(limits={"max_items": 5, "max_tokens": 20, "timeout_seconds": 1})
    item = fixture_item()
    calls = []

    def enrich(_request, members, *, max_tokens, timeout_seconds):
        nonlocal now
        assert not tracing_is_enabled()
        calls.append(timeout_seconds)
        now += 2
        return response(members)

    result = execute(
        boundary, {"first": item.canonical_bytes(), "next": another_item(item).canonical_bytes()},
        request=request, enrich=enrich, clock=lambda: now,
    )
    assert calls == [1]
    assert result.outcome == "partial"
    assert result.candidates == ()
    assert [item.reason for item in result.items] == ["time_limit", "time_limit"]
    assert result.reported_tokens.total == 5


@pytest.mark.parametrize("change", ["version", "extra", "foreign", "forged_revision", "forged_hash"])
def test_invalid_provider_output_cannot_produce_candidate(boundary, change):
    item = fixture_item()

    def enrich(_request, members, **_kwargs):
        valid = response(members)
        payload = json.loads(valid.output)
        pin = payload["summary"][0]["sources"][0]
        if change == "version":
            payload["schema_version"] = "2.0"
        elif change == "extra":
            payload["source_url"] = "https://example.invalid/"
        elif change == "foreign":
            pin["source_id"] = str(uuid4())
        elif change == "forged_revision":
            pin["source_revision"] += 1
        else:
            pin["source_record_hash"] = "0" * 64
        return ModelResult(json.dumps(payload).encode(), valid.usage)

    result = execute(boundary, {"bls": item.canonical_bytes()}, enrich=enrich)
    assert result.outcome == "failed"
    assert result.candidates == ()
    assert result.items[0].reason == "invalid_enrichment"
    assert result.reported_tokens.total == 5


def test_missing_provenance_and_undated_item_have_distinct_dispositions(boundary):
    payload = fixture_item().model_dump(mode="json")
    payload["source"].update(locator=None, excerpt=None)
    missing = json.dumps(payload).encode()
    payload["source"].update(published_at=None, locator="Release heading", publication_time_reason="Missing date")
    undated = json.dumps(payload).encode()
    result = execute(boundary, {"missing": missing, "undated": undated})
    assert result.outcome == "partial"
    assert result.candidates == ()
    assert [(item.status, item.reason) for item in result.items] == [
        ("failed", "invalid_provenance"), ("withheld", "no_publication_time"),
    ]


def test_same_candidate_has_one_delivery_and_synthetic_policy_still_rejects_it(boundary):
    store, _ = boundary
    item = fixture_item()
    batch = {"bls": item.canonical_bytes()}
    first = execute(boundary, batch)
    repeated = execute(boundary, batch)
    assert first.candidates == repeated.candidates
    assert repeated.items[0].reason == "already_delivered"
    (ref,) = first.candidates
    candidate = StoryCandidate.model_validate_json(store.read(ref))
    assert len(scout_state(store).events(candidate.story_id)) == 2

    _, synthetic_policy = review_policy(store.root.parent, store.conn)
    assert synthetic_policy.validate("candidate", ref).report.ready is False
    with pytest.raises(PolicyDenied):
        scout_state(store).transition(TransitionRequest(
            story_id=candidate.story_id, target=StoryState.APPROVED_FOR_PUBLISH, expected_version=2,
            caller_scope="test", idempotency_key="approval-bypass",
        ))


def test_workflow_and_novelty_reject_mismatched_story_and_unpinned_source(boundary):
    store, _ = boundary
    item = fixture_item()
    first = execute(boundary, {"bls": item.canonical_bytes()})
    (ref,) = first.candidates
    state = scout_state(store)
    with pytest.raises(PolicyDenied):
        state.create(uuid4(), first.run_id, ref, "test", "wrong-story")

    second = execute(boundary, {"another": another_item(item).canonical_bytes()})
    (another_ref,) = second.candidates
    another = StoryCandidate.model_validate_json(store.read(another_ref))
    with pytest.raises(PolicyDenied):
        state.transition(TransitionRequest(
            story_id=another.story_id, target=StoryState.SCORED, expected_version=2,
            caller_scope="test", idempotency_key="fake-next-stage", artifact=ref,
        ))

    payload = json.loads(store.read(ref))
    payload["members"][0]["source"]["locator"] = "A different locator changes the pin"
    with pytest.raises(ValidationError):
        StoryCandidate.model_validate_json(json.dumps(payload))
    broken = store.put(json.dumps(payload).encode(), "application/json", "1.0")
    assert not state.policy.validate("cluster", broken).report.ready


def test_cluster_checkpoint_requires_same_candidate_as_discovery(boundary):
    store, _ = boundary
    first = execute(boundary, {"bls": fixture_item().canonical_bytes()})
    (ref,) = first.candidates
    candidate = StoryCandidate.model_validate_json(store.read(ref))
    cluster_id = uuid4()
    story_id = uuid5(cluster_id, "story")

    pending = candidate.model_copy(update={"cluster_id": cluster_id, "story_id": story_id})
    pending_ref = store.put(pending.canonical_bytes(), "application/json", "1.0")
    changed = pending.model_copy(update={"entities": ("Different editorial entities",)})
    changed_ref = store.put(changed.canonical_bytes(), "application/json", "1.0")
    state = scout_state(store)
    assert state.create(story_id, first.run_id, pending_ref, "test", "discovered").state == StoryState.DISCOVERED

    with pytest.raises(PolicyDenied, match="same artifact"):
        state.transition(TransitionRequest(
            story_id=story_id, target=StoryState.CLUSTERED, expected_version=1,
            caller_scope="test", idempotency_key="changed-cluster", artifact=changed_ref,
        ))
    assert len(state.events(story_id)) == 1


def test_reported_token_overrun_blocks_exact_artifact_and_stops_subsequent_calls(boundary):
    calls = []
    item = fixture_item()

    def overrun(_request, members, *, max_tokens, timeout_seconds):
        calls.append(max_tokens)
        return ModelResult(response(members).output, ScoutTokenUsage(input_tokens=3, output_tokens=3))

    output = execute(
        boundary, {"first": item.canonical_bytes(), "second": another_item(item).canonical_bytes()},
        request=run_request(limits={"max_items": 2, "max_tokens": 5, "timeout_seconds": 30}), enrich=overrun,
    )

    assert output.outcome == "partial"
    assert output.candidates == ()
    assert [(item.status, item.reason) for item in output.items] == [
        ("failed", "model_token_overrun"), ("unprocessed", "token_limit"),
    ]
    assert calls == [5]
    assert output.reported_tokens.total == 6


def test_late_empty_feed_reports_timeout(boundary):
    now = 0.0
    store, telemetry = boundary
    request = run_request(limits={"max_items": 5, "max_tokens": 20, "timeout_seconds": 1})

    def fetch(_request, *, timeout_seconds):
        nonlocal now
        assert timeout_seconds == 1
        now = 2.0
        return {}

    result = run_scout(
        request, store=store, telemetry=telemetry, fetch=fetch,
        enrich=lambda _request, members, **_kwargs: response(members), clock=lambda: now,
    )
    assert result.outcome == "partial"
    assert result.issue == "time_limit"


def test_run_and_invocation_identities_remain_bound_to_the_first_request(boundary):
    first = run_request()
    assert execute(boundary, {}, request=first).outcome == "empty"
    assert execute(boundary, {}, request=first).outcome == "empty"
    changed = first.model_copy(update={"window_end": first.window_end + (first.window_end - first.window_start)})
    with pytest.raises(ValueError, match="already bound"):
        execute(boundary, {}, request=changed)
    reused_invocation = first.model_copy(update={"run_id": uuid4()})
    with pytest.raises(ValueError, match="already bound"):
        execute(boundary, {}, request=reused_invocation)


def test_graph_does_not_inherit_programmatic_langsmith_callbacks(boundary):
    token = tracing_v2_callback_var.set(object())
    try:
        output = execute(boundary, {"bls": fixture_item().canonical_bytes()})
    finally:
        tracing_v2_callback_var.reset(token)
    assert output.outcome == "complete"
