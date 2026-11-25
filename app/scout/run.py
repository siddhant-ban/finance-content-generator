"""A bounded on-demand Scout with injectable recorded or live transports."""
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextvars import Context
from dataclasses import dataclass
import json
import threading
import time
from typing import Literal, Protocol, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context
from opentelemetry import context as otel_context
from pydantic import JsonValue, ValidationError

from app.contracts.common import ArtifactRef
from app.contracts.scout import (
    SCOUT_PROJECT, SCOUT_SCHEMA, ItemReason, ItemStatus, RunIssue, ScoutEnrichment,
    ScoutFeedItem, ScoutFeedPoll, ScoutItemDisposition, ScoutMember, ScoutRunInput, ScoutRunOutput,
    ScoutTokenUsage, StoryCandidate, candidate_fingerprint, run_outcome, source_ref,
)
from app.observability.tracing import Telemetry
from app.scout.cluster import cluster_items, combine_warnings
from app.scout.delivery import (
    ReplayInputSnapshot,
    deliver_story_candidate,
    load_replay_response,
    load_saved_run_output,
    record_replay_inputs,
    record_run_lineage,
    record_run_output,
    recover_pending_deliveries,
)
from app.scout.workflow import resolve_story_identity, scout_state
from app.storage.artifacts import ArtifactStore
from app.workflow.state import StateService

_ARTIFACT_WRITE_LOCKS_GUARD = threading.Lock()
_ARTIFACT_WRITE_LOCKS: dict[str, threading.Lock] = {}


def _put_artifact(store: ArtifactStore, data: bytes, media_type: str, schema_version: str) -> ArtifactRef:
    key = str(store.root)
    with _ARTIFACT_WRITE_LOCKS_GUARD:
        lock = _ARTIFACT_WRITE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        return store.put(data, media_type, schema_version)

CLUSTER_NAMESPACE = UUID("cb0d6d66-f933-4ea8-b2e1-192ddcd38684")
SOURCE_ORDER = {"primary": 0, "reputable_secondary": 1, "commentary": 2, "unknown": 3}
FeedPayload = bytes | JsonValue


class ScoutRequestConflict(ValueError):
    """A run or invocation identity is already bound to a different request."""


FeedItemProblem = Literal["malformed_feed_item", "duplicate_feed_item"]


@dataclass(frozen=True)
class FeedBatch:
    items: Mapping[str, FeedPayload]
    polls: tuple[ScoutFeedPoll, ...]
    problems: Mapping[str, FeedItemProblem]


class FeedTransport(Protocol):
    def __call__(self, request: ScoutRunInput, *, timeout_seconds: float) -> Mapping[str, FeedPayload] | FeedBatch:
        """Return normalized items and optional registry poll accounting."""
        ...


class Clusterer(Protocol):
    def __call__(self, request: ScoutRunInput, items: Mapping[str, ScoutMember], *,
                 timeout_seconds: float) -> Mapping[str, tuple[str, ...]]:
        """Partition eligible item IDs using stable cluster keys."""
        ...


@dataclass(frozen=True)
class ModelResult:
    output: bytes
    usage: ScoutTokenUsage


class TokenBudgetExceeded(ValueError):
    """The adapter could not fit a call into its total input and output token ceiling."""


class Enricher(Protocol):
    def __call__(self, request: ScoutRunInput, members: tuple[ScoutMember, ...], *,
                 max_tokens: int, timeout_seconds: float) -> ModelResult:
        """Bound the full request before calling and report its actual token usage."""
        ...


def exact_clusters(request: ScoutRunInput, items: Mapping[str, ScoutMember], *,
                   timeout_seconds: float) -> Mapping[str, tuple[str, ...]]:
    """Use exact canonical URL identity for the recorded Scout slice."""
    groups: dict[str, list[str]] = defaultdict(list)
    for item_id, member in items.items():
        groups[member.source.canonical_url].append(item_id)
    return {key: tuple(ids) for key, ids in groups.items()}


class _GraphState(TypedDict, total=False):
    order: tuple[str, ...]
    eligible: dict[str, ScoutMember]
    clusters: dict[str, tuple[str, ...]]
    dispositions: dict[str, ScoutItemDisposition]
    issue: RunIssue | None
    tokens: ScoutTokenUsage
    feeds: tuple[ScoutFeedPoll, ...]


def _disposition(item_id: str, status: ItemStatus, reason: ItemReason,
                 ref: ArtifactRef | None = None) -> ScoutItemDisposition:
    return ScoutItemDisposition(item_id=item_id, status=status, reason=reason, candidate_ref=ref)


def _snapshot_rejected_item(item_order: int, item_id: str, payload: FeedPayload, reason: ItemReason) -> ReplayInputSnapshot:
    try:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload)
        item = ScoutFeedItem.model_validate_json(raw)
        source = item.source
        resolved_reason = "metadata_only_rights" if source.rights == "metadata_only" else reason
        return ReplayInputSnapshot.unretained(
            item_order=item_order,
            item_id=item_id,
            rights=source.rights,
            retention_reason=resolved_reason,
            source_id=source.source_id,
            source_revision=source.revision,
            source_record_hash=source.record_hash(),
            canonical_url=source.canonical_url,
        )
    except (TypeError, ValueError):
        return ReplayInputSnapshot.unretained(
            item_order=item_order,
            item_id=item_id,
            rights="unparsed",
            retention_reason=reason,
        )


def _eligible(payload: FeedPayload, request: ScoutRunInput) -> ScoutMember | ItemReason:
    try:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload)
        item = ScoutFeedItem.model_validate_json(raw)
        source = item.source
        if source.published_at is None:
            return "no_publication_time"
        if not request.window_start <= source.published_at < request.window_end:
            return "outside_window"
        if source.rights == "unknown":
            return "unknown_rights"
        return ScoutMember(headline=item.headline, source=source, ref=source_ref(source))
    except (TypeError, ValueError):
        return "invalid_provenance"


def _member_order(member: ScoutMember) -> tuple[int, str, str, str]:
    return (SOURCE_ORDER[member.source.source_class], member.source.canonical_url,
            member.ref.source_record_hash, member.headline)


@dataclass
class _Run:
    request: ScoutRunInput
    store: ArtifactStore
    workflow: StateService
    telemetry: Telemetry
    fetch: FeedTransport
    cluster: Clusterer
    enrich: Enricher
    trace_id: str
    deadline: float
    clock: Callable[[], float]

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def fetch_items(self, state: _GraphState) -> _GraphState:
        request = self.request
        timeout = self.remaining()
        if not timeout:
            return {"issue": "time_limit"}
        try:
            with self.telemetry.span("tool.call", SCOUT_PROJECT, {
                "run.id": request.run_id, "tool.ref": request.configuration.feed_snapshot,
            }) as span:
                fetched = self.fetch(request, timeout_seconds=timeout)
                batch = fetched.items if isinstance(fetched, FeedBatch) else fetched
                span.set_attribute("source.count", len(batch))
        except TimeoutError:
            return {"issue": "time_limit"}
        except OSError:
            return {"issue": "feed_unavailable"}
        if any(not isinstance(key, str) or not key.strip() for key in batch):
            return {"issue": "invalid_feed_batch"}

        expired = not self.remaining()
        dispositions: dict[str, ScoutItemDisposition] = {}
        eligible: dict[str, ScoutMember] = {}
        replay_snapshots: list[ReplayInputSnapshot] = []
        for index, item_id in enumerate(batch):
            if index >= request.limits.max_items:
                dispositions[item_id] = _disposition(item_id, "unprocessed", "item_limit")
                replay_snapshots.append(
                    ReplayInputSnapshot.unretained(
                        item_order=index, item_id=item_id, rights="unparsed", retention_reason="item_limit",
                    )
                )
            elif expired or not self.remaining():
                dispositions[item_id] = _disposition(item_id, "unprocessed", "time_limit")
                replay_snapshots.append(
                    ReplayInputSnapshot.unretained(
                        item_order=index, item_id=item_id, rights="unparsed", retention_reason="time_limit",
                    )
                )
            elif isinstance(fetched, FeedBatch) and item_id in fetched.problems:
                problem = fetched.problems[item_id]
                dispositions[item_id] = _disposition(
                    item_id, "withheld" if problem == "duplicate_feed_item" else "failed", problem,
                )
                replay_snapshots.append(
                    ReplayInputSnapshot.unretained(
                        item_order=index, item_id=item_id, rights="unparsed", retention_reason=problem,
                    )
                )
            else:
                member = _eligible(batch[item_id], request)
                if isinstance(member, ScoutMember):
                    eligible[item_id] = member
                    replay_snapshots.append(
                        ReplayInputSnapshot.from_feed_item(
                            item_order=index,
                            item_id=item_id,
                            item=ScoutFeedItem(headline=member.headline, source=member.source),
                        )
                    )
                else:
                    status: ItemStatus = "failed" if member == "invalid_provenance" else "withheld"
                    dispositions[item_id] = _disposition(item_id, status, member)
                    replay_snapshots.append(_snapshot_rejected_item(index, item_id, batch[item_id], member))
        if replay_snapshots:
            record_replay_inputs(self.store.conn, run_id=request.run_id, items=replay_snapshots)
        result: _GraphState = {
            "order": tuple(batch), "eligible": eligible, "dispositions": dispositions,
            "issue": "time_limit" if expired else None,
        }
        if isinstance(fetched, FeedBatch):
            result["feeds"] = fetched.polls
        return result

    def group_items(self, state: _GraphState) -> _GraphState:
        eligible = state.get("eligible", {})
        if not eligible:
            return {}
        timeout = self.remaining()
        if not timeout:
            return self.skip_group(state, "unprocessed", "time_limit")

        try:
            groups = dict(self.cluster(self.request, eligible.copy(), timeout_seconds=timeout))
            members = [item_id for ids in groups.values() for item_id in ids]
            if (any(not isinstance(key, str) or not key.strip() or not isinstance(ids, tuple) or not ids
                    for key, ids in groups.items()) or len(members) != len(eligible)
                    or set(members) != set(eligible)):
                raise ValueError("clusterer must partition its eligible inputs")
        except TimeoutError:
            return self.skip_group(state, "unprocessed", "time_limit")
        except (ValueError, TypeError, OSError):
            return self.skip_group(state, "failed", "cluster_failed")
        return {"clusters": groups}

    @staticmethod
    def skip_group(state: _GraphState, status: ItemStatus, reason: Literal["cluster_failed", "time_limit"]) -> _GraphState:
        dispositions = state.get("dispositions", {}) | {
            item_id: _disposition(item_id, status, reason)
            for item_id in state.get("eligible", {})
        }
        return {"dispositions": dispositions, "issue": reason}

    def process_clusters(self, state: _GraphState) -> _GraphState:
        dispositions = state.get("dispositions", {}).copy()
        input_tokens = output_tokens = 0
        budget_unknown = False
        order = {item_id: index for index, item_id in enumerate(state.get("order", ()))}
        clusters = sorted(state.get("clusters", {}).items(), key=lambda pair: min(order[item_id] for item_id in pair[1]))
        eligible = state.get("eligible", {})
        for key, ids in clusters:
            tokens_left = self.request.limits.max_tokens - input_tokens - output_tokens
            timeout = self.remaining()
            ref = None
            if not timeout:
                status, reason = "unprocessed", "time_limit"
            elif tokens_left <= 0 or budget_unknown:
                status, reason = "unprocessed", "token_limit"
            else:
                status, reason, ref, usage = self.process_cluster(
                    key, tuple(eligible[item_id] for item_id in ids), tokens_left,
                )
                if usage is None:
                    budget_unknown = True
                else:
                    input_tokens += usage.input_tokens
                    output_tokens += usage.output_tokens
            dispositions.update((item_id, _disposition(item_id, status, reason, ref)) for item_id in ids)
        return {
            "dispositions": dispositions,
            "tokens": ScoutTokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        }

    def process_cluster(
        self, key: str, members: tuple[ScoutMember, ...], max_tokens: int
    ) -> tuple[ItemStatus, ItemReason, ArtifactRef | None, ScoutTokenUsage | None]:
        request = self.request
        zero = ScoutTokenUsage(input_tokens=0, output_tokens=0)
        by_revision: dict[tuple[UUID, int], ScoutMember] = {}
        for member in sorted(members, key=_member_order):
            identity = (member.ref.source_id, member.ref.source_revision)
            first = by_revision.setdefault(identity, member)
            if first.ref != member.ref:
                return "failed", "invalid_provenance", None, zero
        sources = tuple(by_revision.values())
        fingerprint = candidate_fingerprint(sources)
        if fingerprint in request.fingerprints.known:
            return "unchanged", "known_fingerprint", None, zero

        timeout = self.remaining()
        if not timeout:
            return "unprocessed", "time_limit", None, zero
        story_id, cluster_id, latest_revision, latest_fingerprint, conflict = resolve_story_identity(
            self.store.conn, sources, key, request.configuration.cluster_version, CLUSTER_NAMESPACE,
        )
        if conflict:
            return "failed", "revision_conflict", None, zero
        if latest_revision == 0:
            target_revision = 1
        elif latest_fingerprint == fingerprint:
            target_revision = latest_revision
        else:
            target_revision = latest_revision + 1
        saved_response = load_replay_response(
            self.store.conn, run_id=request.run_id, story_id=story_id, revision=target_revision,
        )
        if saved_response is not None and saved_response.fingerprint == fingerprint:
            result = ModelResult(saved_response.response_json.encode("utf-8"), saved_response.usage)
            usage = saved_response.usage
        else:
            try:
                with self.telemetry.span("model.call", SCOUT_PROJECT, {
                    "run.id": request.run_id, "story.id": story_id, "model.parameters.max_tokens": max_tokens,
                    "source.count": len(sources), "retry.count": 0,
                }) as span:
                    result = self.enrich(request, sources, max_tokens=max_tokens, timeout_seconds=timeout)
                    usage = ScoutTokenUsage.model_validate(result.usage.model_dump())
                    span.set_attributes({"usage.input_tokens": usage.input_tokens, "usage.output_tokens": usage.output_tokens})
            except TokenBudgetExceeded:
                return "unprocessed", "token_limit", None, zero
            except TimeoutError:
                return "unprocessed", "time_limit", None, None
            except (OSError, ValueError):
                return "failed", "model_unavailable", None, None
        if usage.total > max_tokens:
            return "failed", "model_token_overrun", None, usage
        if not self.remaining():
            return "unprocessed", "time_limit", None, usage

        try:
            enrichment = ScoutEnrichment.model_validate_json(result.output)
            warnings = combine_warnings(enrichment.warnings, sources)
            candidate = StoryCandidate(
                **(enrichment.model_dump() | {"warnings": warnings}),
                schema_id=SCOUT_SCHEMA, kind="editorial",
                story_id=story_id, cluster_id=cluster_id, revision=target_revision, fingerprint=fingerprint,
                representative=sources[0].ref, members=sources,
            )
        except ValidationError:
            return "failed", "invalid_enrichment", None, usage
        if not set(candidate.topics) <= set(request.topic_policy.topics):
            return "withheld", "outside_topic_policy", None, usage

        for _ in range(5):
            if not self.remaining():
                return "unprocessed", "time_limit", None, usage
            ref = _put_artifact(self.store, candidate.canonical_bytes(), "application/json", candidate.schema_version)
            if not self.remaining():
                return "unprocessed", "time_limit", None, usage
            with self.telemetry.span("workflow.transition", SCOUT_PROJECT, {
                "run.id": request.run_id, "story.id": story_id, "artifact.hash": ref.sha256,
            }):
                outcome = deliver_story_candidate(
                    self.store,
                    self.workflow,
                    request=request,
                    candidate=candidate,
                    ref=ref,
                    cluster_key=key,
                    trace_id=self.trace_id,
                    response_bytes=result.output,
                    usage=usage,
                )
            if not outcome.retry_with_next_revision:
                return outcome.status, outcome.reason, outcome.candidate_ref, usage
            story_id, cluster_id, latest_revision, latest_fingerprint, conflict = resolve_story_identity(
                self.store.conn, sources, key, request.configuration.cluster_version, CLUSTER_NAMESPACE,
            )
            if conflict:
                return "failed", "revision_conflict", None, usage
            next_revision = 1 if latest_revision == 0 else (
                latest_revision if latest_fingerprint == fingerprint else latest_revision + 1
            )
            candidate = StoryCandidate(
                **(enrichment.model_dump() | {"warnings": warnings}),
                schema_id=SCOUT_SCHEMA, kind="editorial",
                story_id=story_id, cluster_id=cluster_id, revision=next_revision, fingerprint=fingerprint,
                representative=sources[0].ref, members=sources,
            )
        return "failed", "revision_conflict", None, usage


def _pin_request(store: ArtifactStore, request: ScoutRunInput) -> ArtifactRef:
    if store.conn.in_transaction:
        raise ValueError("Scout requires an idle journal connection")
    ref = _put_artifact(store, request.canonical_bytes(), "application/json", request.schema_version)
    identity = (str(request.run_id), str(request.invocation_id), ref.sha256)
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        store.conn.execute(
            "INSERT INTO scout_run_requests(run_id, invocation_id, input_sha256) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING", identity,
        )
        row = store.conn.execute(
            "SELECT run_id, invocation_id, input_sha256 FROM scout_run_requests WHERE run_id = ?",
            (identity[0],),
        ).fetchone()
        if row is None or tuple(row) != identity:
            raise ScoutRequestConflict("Scout run or invocation identity is already bound to another request")
        store.conn.commit()
    except BaseException:
        store.conn.rollback()
        raise
    return ref


def run_scout(
    request: ScoutRunInput, *, store: ArtifactStore, telemetry: Telemetry, fetch: FeedTransport,
    enrich: Enricher, cluster: Clusterer = cluster_items, clock: Callable[[], float] = time.monotonic,
) -> ScoutRunOutput:
    """Persist frozen inputs, run the discovery graph, and persist its batch envelope."""
    request = ScoutRunInput.model_validate(request.model_dump())
    deadline = clock() + request.limits.timeout_seconds
    input_ref = _pin_request(store, request)
    saved_output = load_saved_run_output(store, request.run_id)
    if saved_output is not None:
        recover_pending_deliveries(store, run_id=request.run_id)
        if saved_output.input_ref != input_ref:
            raise ScoutRequestConflict("Saved Scout output does not match pinned input")
        return saved_output
    config = request.configuration
    with telemetry.span("agent.invoke", SCOUT_PROJECT, {
        "run.id": request.run_id, "invocation.id": request.invocation_id, "agent.name": "news_scout",
        "agent.version": "1.0", "input.ref": input_ref.sha256, "config.hash": config.record_hash(),
        "config.version": config.version, "prompt.hash": config.prompt_hash, "prompt.version": config.prompt_version,
        "model.provider": config.provider, "model.name": config.model,
        "model.parameters": {"temperature": config.temperature, "max_tokens": request.limits.max_tokens},
        "retry.count": 0,
    }) as span:
        trace_id = f"{span.get_span_context().trace_id:032x}"
        record_run_lineage(
            store.conn,
            run_id=request.run_id,
            invocation_id=request.invocation_id,
            replay_mode="live",
            source_run_id=None,
            source_input_sha256=None,
            input_sha256=input_ref.sha256,
            config_hash=config.record_hash(),
            prompt_hash=config.prompt_hash,
            provider=config.provider,
            model=config.model,
            experiment_id=None,
            trace_id=trace_id,
        )
        worker = _Run(request, store, scout_state(store), telemetry, fetch, cluster, enrich,
                      trace_id, deadline, clock)
        graph = StateGraph(_GraphState)
        graph.add_node("fetch", worker.fetch_items)
        graph.add_node("cluster", worker.group_items)
        graph.add_node("enrich_and_deliver", worker.process_clusters)
        graph.add_edge(START, "fetch")
        graph.add_edge("fetch", "cluster")
        graph.add_edge("cluster", "enrich_and_deliver")
        graph.add_edge("enrich_and_deliver", END)
        # Carry only the sanitized telemetry parent into LangGraph. Ambient
        # LangChain callbacks could otherwise receive raw source and model text.
        telemetry_context = otel_context.get_current()

        def invoke() -> _GraphState:
            token = otel_context.attach(telemetry_context)
            try:
                with tracing_context(enabled=False, parent=False):
                    return cast(_GraphState, graph.compile().invoke({}, {"recursion_limit": 5}))
            finally:
                otel_context.detach(token)

        result = Context().run(invoke)
        if "dispositions" not in result or "tokens" not in result:
            raise RuntimeError("Scout graph ended before finalizing its report")
        items = tuple(result["dispositions"][key] for key in result.get("order", ()))
        issue = result.get("issue")
        feeds = result.get("feeds")
        output = ScoutRunOutput(
            schema_version="1.0" if feeds is None else "1.1",
            run_id=request.run_id, invocation_id=request.invocation_id,
            input_ref=input_ref, trace_id=trace_id, outcome=run_outcome(items, issue, feeds),
            candidates=tuple(dict.fromkeys(item.candidate_ref for item in items if item.candidate_ref)),
            items=items, issue=issue, reported_tokens=result["tokens"], feeds=feeds,
        )
        output_ref = _put_artifact(store, output.canonical_bytes(), "application/json", "1.0")
        record_run_output(store.conn, run_id=request.run_id, output_sha256=output_ref.sha256, outcome=output.outcome)
        status = {"failed": "FAILED", "partial": "BLOCKED"}.get(output.outcome, "OK")
        span.set_attributes({"output.ref": output_ref.sha256, "result.status": status})
        return output
