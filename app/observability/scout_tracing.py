"""Sanitized OpenTelemetry span hierarchy helpers for News Scout."""
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from uuid import UUID

from opentelemetry.trace import StatusCode

from app.contracts.common import ArtifactRef
from app.contracts.scout import (
    SCOUT_PROJECT,
    ItemReason,
    ItemStatus,
    ScoutFeedItem,
    ScoutFeedPoll,
    ScoutRunInput,
    ScoutRunOutput,
)
from app.observability.sanitize import sanitize_attributes
from app.observability.tracing import Telemetry

_ACTIVE_TELEMETRY: ContextVar[Telemetry | None] = ContextVar("scout_active_telemetry", default=None)
_ACTIVE_REQUEST: ContextVar[ScoutRunInput | None] = ContextVar("scout_active_request", default=None)
_FETCH_COUNTER: ContextVar[list[int] | None] = ContextVar("scout_fetch_counter", default=None)

_VALID_SOURCE_CLASSES = frozenset(("primary", "reputable_secondary", "commentary", "unknown"))


class _NoopSpan:
    def set_attribute(self, key: str, value: object) -> None:
        pass

    def set_attributes(self, values: Mapping[str, object]) -> None:
        pass

    def set_status(self, status: object) -> None:
        pass


class _DispatchSpans:
    def __init__(self, transition_span, dispatch_span) -> None:
        self._transition = transition_span
        self._dispatch = dispatch_span

    def finalize(self, delivery_status: str, *, error_code: str | None = None) -> None:
        attrs: dict[str, object] = {"delivery.status": delivery_status}
        if error_code is not None:
            attrs["error.code"] = error_code
        for span in (self._transition, self._dispatch):
            span.set_attributes(attrs)
            if error_code is not None:
                span.set_status(StatusCode.ERROR)


def current_telemetry() -> Telemetry | None:
    """Return the Telemetry instance bound to the active Scout run, if any."""
    return _ACTIVE_TELEMETRY.get()


def current_scout_request() -> ScoutRunInput | None:
    """Return the ScoutRunInput bound to the active Scout run, if any."""
    return _ACTIVE_REQUEST.get()


def safe_item_id(item_id: str) -> str:
    """Return a sanitized bounded item identifier without leaking URLs or secrets."""
    if sanitize_attributes({"item.id": item_id}):
        return item_id
    return "item:" + sha256(item_id.encode("utf-8", errors="replace")).hexdigest()[:16]


def extract_source_class(payload: object) -> str:
    """Best-effort extraction of source_class from a feed item without raising."""
    try:
        if isinstance(payload, bytes):
            raw = json.loads(payload.decode("utf-8"))
        elif isinstance(payload, str):
            raw = json.loads(payload)
        elif isinstance(payload, Mapping):
            raw = payload
        else:
            return "unknown"
        if isinstance(raw, Mapping):
            source = raw.get("source")
            if isinstance(source, Mapping):
                cls = source.get("source_class")
                if isinstance(cls, str) and cls in _VALID_SOURCE_CLASSES:
                    return cls
        item = ScoutFeedItem.model_validate(raw)
        return item.source.source_class
    except (ValueError, TypeError):
        return "unknown"


def map_model_error_code(reason: str) -> str:
    """Map Scout model/enrichment failure reasons onto sanitized error codes."""
    mapping = {
        "time_limit": "timeout",
        "token_limit": "token_limit",
        "model_unavailable": "model_unavailable",
        "model_token_overrun": "model_token_overrun",
        "invalid_enrichment": "invalid_enrichment",
        "revision_conflict": "conflict",
    }
    return mapping.get(reason, "unavailable")


def _correlation_attrs(request: ScoutRunInput | None) -> dict[str, object]:
    if request is None:
        return {}
    cfg = request.configuration
    return {
        "run.id": request.run_id,
        "release.id": request.release_id,
        "prompt.version": cfg.prompt_version,
        "prompt.hash": cfg.prompt_hash,
        "model.provider": cfg.provider,
        "tool.ref": cfg.feed_snapshot,
    }


@contextmanager
def bind_scout_context(telemetry: Telemetry, request: ScoutRunInput) -> Iterator[None]:
    """Bind active telemetry and Scout request inside an isolated ContextVar frame."""
    tel_token = _ACTIVE_TELEMETRY.set(telemetry)
    req_token = _ACTIVE_REQUEST.set(request)
    try:
        yield
    finally:
        _ACTIVE_REQUEST.reset(req_token)
        _ACTIVE_TELEMETRY.reset(tel_token)


@contextmanager
def scout_run_span(
    telemetry: Telemetry,
    request: ScoutRunInput,
    *,
    input_ref: ArtifactRef,
) -> Iterator[tuple[object, str]]:
    """Open the root agent.invoke span for a Scout run and bind active telemetry context."""
    cfg = request.configuration
    attrs = {
        "run.id": request.run_id,
        "invocation.id": request.invocation_id,
        "agent.name": "news_scout",
        "agent.version": "1.0",
        "input.ref": input_ref.sha256,
        "config.hash": cfg.record_hash(),
        "config.version": cfg.version,
        "prompt.hash": cfg.prompt_hash,
        "prompt.version": cfg.prompt_version,
        "model.provider": cfg.provider,
        "model.name": cfg.model,
        "model.parameters": {"temperature": cfg.temperature, "max_tokens": request.limits.max_tokens},
        "release.id": request.release_id,
        "tool.ref": cfg.feed_snapshot,
        "retry.count": 0,
    }
    with bind_scout_context(telemetry, request):
        with telemetry.span("agent.invoke", SCOUT_PROJECT, attrs) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            yield span, trace_id


def finalize_scout_run_span(span, output: ScoutRunOutput, output_ref: ArtifactRef) -> None:
    """Attach final typed outcome and artifact reference to the root Scout span."""
    status = {"failed": "FAILED", "partial": "BLOCKED"}.get(output.outcome, "OK")
    span.set_attributes(
        {
            "output.ref": output_ref.sha256,
            "result.status": status,
            "run.outcome": output.outcome,
            "source.count": len(output.items),
        }
    )
    if output.outcome == "failed":
        span.set_status(StatusCode.ERROR)


@contextmanager
def scout_fetch_scope() -> Iterator[list[int]]:
    """Track whether child scout.fetch spans were emitted by the feed transport."""
    counter = [0]
    token = _FETCH_COUNTER.set(counter)
    try:
        yield counter
    finally:
        _FETCH_COUNTER.reset(token)


@contextmanager
def scout_fetch_span(
    telemetry: Telemetry | None,
    *,
    run_id: UUID | None,
    feed_id: str,
    feed_revision: int,
    feed_snapshot: str,
    request: ScoutRunInput | None = None,
) -> Iterator[object]:
    """Emit a child scout.fetch span for a single feed poll."""
    if telemetry is None:
        yield _NoopSpan()
        return
    counter = _FETCH_COUNTER.get()
    if counter is not None:
        counter[0] += 1
    attrs: dict[str, object] = {
        **_correlation_attrs(request),
        "feed.id": feed_id,
        "feed.revision": feed_revision,
        "tool.ref": feed_snapshot,
    }
    if run_id is not None:
        attrs["run.id"] = run_id
    with telemetry.span("scout.fetch", SCOUT_PROJECT, attrs) as span:
        yield span


def finalize_fetch_span(span, poll: ScoutFeedPoll, *, source_count: int) -> None:
    """Attach poll outcome, reason, and item count to a scout.fetch span."""
    attrs: dict[str, object] = {
        "feed.id": poll.feed_id,
        "feed.revision": poll.revision,
        "feed.status": poll.status,
        "source.count": source_count,
    }
    if poll.reason is not None:
        attrs["feed.reason"] = poll.reason
    span.set_attributes(attrs)
    if poll.status in {"unavailable", "limited"}:
        span.set_status(StatusCode.ERROR)


def emit_item_span(
    telemetry: Telemetry,
    *,
    request: ScoutRunInput,
    item_id: str,
    status: ItemStatus,
    reason: ItemReason,
    source_class: str,
    story_id: UUID | None = None,
    story_revision: int | None = None,
    artifact_hash: str | None = None,
    source_refs: tuple[str, ...] = (),
) -> None:
    """Emit a child scout.item span recording normalized item disposition."""
    attrs: dict[str, object] = {
        **_correlation_attrs(request),
        "item.id": safe_item_id(item_id),
        "item.status": status,
        "item.reason": reason,
        "source.class": source_class if source_class in _VALID_SOURCE_CLASSES else "unknown",
    }
    if story_id is not None:
        attrs["story.id"] = story_id
    if story_revision is not None:
        attrs["story.revision"] = story_revision
    if artifact_hash is not None:
        attrs["artifact.hash"] = artifact_hash
    if source_refs:
        attrs["source.refs"] = list(source_refs)
    with telemetry.span("scout.item", SCOUT_PROJECT, attrs) as span:
        if status == "failed":
            span.set_status(StatusCode.ERROR)


@contextmanager
def scout_model_span(
    telemetry: Telemetry,
    *,
    request: ScoutRunInput,
    story_id: UUID,
    story_revision: int,
    max_tokens: int,
    source_count: int,
    source_refs: tuple[str, ...] = (),
    retry_count: int = 0,
) -> Iterator[object]:
    """Open a child model.call span with pinned model, prompt, and correlation metadata."""
    cfg = request.configuration
    attrs: dict[str, object] = {
        **_correlation_attrs(request),
        "story.id": story_id,
        "story.revision": story_revision,
        "model.name": cfg.model,
        "model.parameters": {"temperature": cfg.temperature, "max_tokens": max_tokens},
        "source.count": source_count,
        "retry.count": retry_count,
    }
    if source_refs:
        attrs["source.refs"] = list(source_refs)
    with telemetry.span("model.call", SCOUT_PROJECT, attrs) as span:
        yield span


def mark_model_span_error(
    span,
    reason: str,
    *,
    availability_reason: str | None = None,
) -> None:
    """Attach sanitized error code and ERROR status to a failed model.call span."""
    attrs: dict[str, object] = {"error.code": map_model_error_code(reason)}
    if availability_reason is not None:
        attrs["usage.availability_reason"] = availability_reason
    span.set_attributes(attrs)
    span.set_status(StatusCode.ERROR)


@contextmanager
def scout_dispatch_span(
    telemetry: Telemetry,
    *,
    request: ScoutRunInput,
    story_id: UUID,
    story_revision: int,
    artifact_hash: str,
    source_refs: tuple[str, ...] = (),
) -> Iterator[_DispatchSpans]:
    """Open linked workflow.transition and scout.dispatch spans for candidate delivery."""
    attrs: dict[str, object] = {
        **_correlation_attrs(request),
        "story.id": story_id,
        "story.revision": story_revision,
        "artifact.hash": artifact_hash,
    }
    if source_refs:
        attrs["source.refs"] = list(source_refs)
    with telemetry.span("workflow.transition", SCOUT_PROJECT, attrs) as transition_span:
        with telemetry.span("scout.dispatch", SCOUT_PROJECT, attrs) as dispatch_span:
            yield _DispatchSpans(transition_span, dispatch_span)
