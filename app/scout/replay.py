"""Offline audit replay and separately identified re-evaluation for News Scout."""
from collections.abc import Callable
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
import socket
from typing import Annotated, Iterator, Literal
import urllib.request
from uuid import UUID, uuid4

import httpx
from pydantic import Field
import requests

from app.contracts.common import ArtifactRef, Nonempty, Sha256, StrictModel
from app.contracts.evidence import Revision
from app.contracts.scout import (
    SCOUT_PROJECT,
    SCOUT_SCHEMA,
    ScoutEnrichment,
    ScoutFeedItem,
    ScoutMember,
    ScoutRunInput,
    ScoutTokenUsage,
    StoryCandidate,
    TraceId,
    candidate_fingerprint,
    source_ref,
)
from app.observability.tracing import Telemetry
from app.scout.cluster import cluster_items, combine_warnings
from app.scout.delivery import (
    InputRights,
    RecordedModelResponse,
    _artifact_ref,
    record_replay_response,
    record_run_lineage,
    record_run_output,
)
from app.scout.eval_config import NonNegativeDecimal
from app.scout.ledger import SpendLedger
from app.scout.run import CLUSTER_NAMESPACE, Clusterer, Enricher, _member_order, _pin_request, _put_artifact
from app.scout.workflow import resolve_story_identity
from app.storage.artifacts import ArtifactStore


@contextmanager
def offline_network_disabled() -> Iterator[None]:
    """Block outbound socket and HTTP client calls during offline replay/reeval."""
    orig_create_connection = socket.create_connection
    orig_socket_connect = socket.socket.connect
    orig_urlopen = urllib.request.urlopen
    orig_httpx_send = httpx.Client.send
    orig_httpx_async_send = httpx.AsyncClient.send
    orig_requests_send = requests.Session.send

    def _blocked(*_args, **_kwargs):
        raise RuntimeError("network disabled during offline Scout replay")

    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.socket.connect = _blocked  # type: ignore[method-assign]
    urllib.request.urlopen = _blocked  # type: ignore[assignment]
    httpx.Client.send = _blocked  # type: ignore[method-assign]
    httpx.AsyncClient.send = _blocked  # type: ignore[method-assign]
    requests.Session.send = _blocked  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.create_connection = orig_create_connection
        socket.socket.connect = orig_socket_connect
        urllib.request.urlopen = orig_urlopen
        httpx.Client.send = orig_httpx_send
        httpx.AsyncClient.send = orig_httpx_async_send
        requests.Session.send = orig_requests_send


class UnreplayableItem(StrictModel):
    """Input item whose retention rights or capture state prevents offline replay."""

    item_id: Nonempty
    rights: InputRights
    reason: Nonempty
    source_id: UUID | None = None
    source_revision: Revision | None = None
    canonical_url: str | None = None


class ReplayCoverage(StrictModel):
    """Explicit summary of replayable vs rights-restricted inputs."""

    total_inputs: int = Field(ge=0)
    replayable_inputs: int = Field(ge=0)
    unreplayable_inputs: int = Field(ge=0)
    status: Literal["complete", "partial", "unavailable", "empty"]
    unreplayable_items: tuple[UnreplayableItem, ...] = ()


class ReplayedCandidate(StrictModel):
    """Candidate reconstructed during offline audit replay or re-evaluation."""

    story_id: UUID
    cluster_id: UUID
    revision: Revision
    cluster_key: Nonempty
    fingerprint: Sha256
    original_artifact_sha256: Sha256 | None = None
    reproduced_artifact_sha256: Sha256
    hash_matches: bool | None = None
    candidate: StoryCandidate
    reported_tokens: ScoutTokenUsage


class ScoutAuditReplayOutput(StrictModel):
    """Strict output envelope for offline deterministic audit replay (zero dispatch/network)."""

    schema_version: Literal["1.0"] = "1.0"
    replay_mode: Literal["audit_replay"] = "audit_replay"
    source_run_id: UUID
    source_invocation_id: UUID
    source_input_sha256: Sha256
    source_output_sha256: Sha256 | None = None
    config_hash: Sha256
    prompt_hash: Sha256
    feed_snapshot: Sha256
    cluster_version: Nonempty
    provider: Nonempty
    model: Nonempty
    network_disabled: Literal[True] = True
    dispatch_disabled: Literal[True] = True
    replay_coverage: ReplayCoverage
    unreplayable_items: tuple[UnreplayableItem, ...]
    candidates: tuple[ReplayedCandidate, ...]
    all_hashes_matched: bool
    reported_tokens: ScoutTokenUsage


class ScoutReevalOutput(StrictModel):
    """Strict output envelope for separately identified model/prompt re-evaluation."""

    schema_version: Literal["1.0"] = "1.0"
    replay_mode: Literal["reeval"] = "reeval"
    run_id: UUID
    invocation_id: UUID
    source_run_id: UUID
    source_invocation_id: UUID
    source_input_sha256: Sha256
    input_sha256: Sha256
    output_sha256: Sha256 | None = None
    experiment_id: Nonempty
    hard_spend_cap_usd: NonNegativeDecimal
    source_config_hash: Sha256
    source_prompt_hash: Sha256
    source_model: Nonempty
    config_hash: Sha256
    prompt_hash: Sha256
    prompt_version: Nonempty
    provider: Nonempty
    model: Nonempty
    temperature: Annotated[float, Field(ge=0, le=2)]
    network_disabled: bool = True
    dispatch_disabled: Literal[True] = True
    replay_coverage: ReplayCoverage
    unreplayable_items: tuple[UnreplayableItem, ...]
    candidates: tuple[ReplayedCandidate, ...]
    reported_tokens: ScoutTokenUsage
    trace_id: TraceId


def _resolve_source_run(
    store: ArtifactStore,
    *,
    run_id: UUID | None,
    story_id: UUID | None,
    revision: int | None,
) -> UUID:
    if run_id is not None:
        return run_id
    if story_id is None:
        raise ValueError("either run_id or story_id is required for replay")
    conn = store.conn
    queries = (
        ("scout_story_revisions", "revision"),
        ("scout_deliveries", "revision"),
        ("scout_replay_responses", "revision"),
    )
    for table, rev_col in queries:
        if revision is not None:
            row = conn.execute(
                f"SELECT run_id FROM {table} WHERE story_id = ? AND {rev_col} = ? LIMIT 1",
                (str(story_id), revision),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT run_id FROM {table} WHERE story_id = ? ORDER BY {rev_col} DESC LIMIT 1",
                (str(story_id),),
            ).fetchone()
        if row is not None:
            return UUID(row[0])
    raise ValueError(f"no recorded Scout run found for story {story_id}")


def _load_source_context(
    store: ArtifactStore, source_run_id: UUID
) -> tuple[ScoutRunInput, ArtifactRef, str | None, dict[str, ScoutMember], dict[str, int], ReplayCoverage]:
    conn = store.conn
    req_row = conn.execute(
        "SELECT input_sha256 FROM scout_run_requests WHERE run_id = ?",
        (str(source_run_id),),
    ).fetchone()
    if req_row is None:
        raise ValueError(f"no frozen request found for run {source_run_id}")
    input_ref = _artifact_ref(conn, req_row[0])
    source_request = ScoutRunInput.model_validate_json(store.read(input_ref))

    out_row = conn.execute(
        "SELECT output_sha256 FROM scout_run_outputs WHERE run_id = ?",
        (str(source_run_id),),
    ).fetchone()
    source_output_sha256 = out_row[0] if out_row is not None else None

    rows = conn.execute(
        "SELECT item_order, item_id, rights, retained, retention_reason, "
        "source_id, source_revision, source_record_hash, canonical_url, payload_sha256, payload_json "
        "FROM scout_replay_inputs WHERE run_id = ? ORDER BY item_order ASC",
        (str(source_run_id),),
    ).fetchall()

    eligible: dict[str, ScoutMember] = {}
    order: dict[str, int] = {}
    unreplayable: list[UnreplayableItem] = []

    for row in rows:
        item_order = int(row[0])
        item_id = row[1]
        rights: InputRights = row[2]
        retained = bool(row[3])
        retention_reason = row[4]
        source_id = UUID(row[5]) if row[5] else None
        source_revision = int(row[6]) if row[6] is not None else None
        canonical_url = row[8]
        payload_sha256 = row[9]
        payload_json = row[10]
        order[item_id] = item_order

        if not retained or payload_json is None or rights != "excerpt_permitted":
            unreplayable.append(
                UnreplayableItem(
                    item_id=item_id,
                    rights=rights,
                    reason=retention_reason,
                    source_id=source_id,
                    source_revision=source_revision,
                    canonical_url=canonical_url,
                )
            )
            continue

        actual_sha = sha256(payload_json.encode("utf-8")).hexdigest()
        if actual_sha != payload_sha256:
            raise ValueError(f"replay input snapshot integrity mismatch for {item_id}")
        item = ScoutFeedItem.model_validate_json(payload_json)
        member = ScoutMember(headline=item.headline, source=item.source, ref=source_ref(item.source))
        eligible[item_id] = member

    total_inputs = len(rows)
    replayable_inputs = len(eligible)
    unreplayable_inputs = len(unreplayable)
    if total_inputs == 0:
        cov_status: Literal["complete", "partial", "unavailable", "empty"] = "empty"
    elif unreplayable_inputs == 0:
        cov_status = "complete"
    elif replayable_inputs == 0:
        cov_status = "unavailable"
    else:
        cov_status = "partial"

    coverage = ReplayCoverage(
        total_inputs=total_inputs,
        replayable_inputs=replayable_inputs,
        unreplayable_inputs=unreplayable_inputs,
        status=cov_status,
        unreplayable_items=tuple(unreplayable),
    )
    return source_request, input_ref, source_output_sha256, eligible, order, coverage


def _load_recorded_responses(
    store: ArtifactStore,
    *,
    source_run_id: UUID,
    story_id: UUID | None = None,
    revision: int | None = None,
) -> tuple[RecordedModelResponse, ...]:
    clauses = ["run_id = ?"]
    params: list[object] = [str(source_run_id)]
    if story_id is not None:
        clauses.append("story_id = ?")
        params.append(str(story_id))
    if revision is not None:
        clauses.append("revision = ?")
        params.append(revision)
    where = " AND ".join(clauses)
    rows = store.conn.execute(
        f"SELECT run_id, story_id, revision, cluster_id, cluster_key, cluster_version, "
        f"fingerprint, response_sha256, response_json, input_tokens, output_tokens, "
        f"candidate_sha256, created_at FROM scout_replay_responses WHERE {where} "
        f"ORDER BY story_id ASC, revision ASC",
        tuple(params),
    ).fetchall()
    return tuple(
        RecordedModelResponse(
            run_id=UUID(row[0]),
            story_id=UUID(row[1]),
            revision=int(row[2]),
            cluster_id=UUID(row[3]),
            cluster_key=row[4],
            cluster_version=row[5],
            fingerprint=row[6],
            response_sha256=row[7],
            response_json=row[8],
            input_tokens=int(row[9]),
            output_tokens=int(row[10]),
            candidate_sha256=row[11],
            created_at=row[12],
        )
        for row in rows
    )


def _cluster_sources(
    request: ScoutRunInput,
    eligible: dict[str, ScoutMember],
    order: dict[str, int],
    cluster: Clusterer,
) -> list[tuple[str, tuple[ScoutMember, ...], str]]:
    if not eligible:
        return []
    groups = dict(cluster(request, eligible.copy(), timeout_seconds=request.limits.timeout_seconds))
    ordered_groups = sorted(groups.items(), key=lambda pair: min(order[item_id] for item_id in pair[1]))
    clusters: list[tuple[str, tuple[ScoutMember, ...], str]] = []
    for key, ids in ordered_groups:
        by_revision: dict[tuple[UUID, int], ScoutMember] = {}
        for member in sorted((eligible[item_id] for item_id in ids), key=_member_order):
            identity = (member.ref.source_id, member.ref.source_revision)
            first = by_revision.setdefault(identity, member)
            if first.ref != member.ref:
                raise ValueError("duplicate source revision conflict in replay input")
        sources = tuple(by_revision.values())
        fingerprint = candidate_fingerprint(sources)
        if fingerprint in request.fingerprints.known:
            continue
        clusters.append((key, sources, fingerprint))
    return clusters


def replay_scout(
    store: ArtifactStore,
    *,
    run_id: UUID | None = None,
    story_id: UUID | None = None,
    revision: int | None = None,
    cluster: Clusterer = cluster_items,
) -> ScoutAuditReplayOutput:
    """Perform read-only offline audit replay without network or downstream dispatch."""
    source_run_id = _resolve_source_run(store, run_id=run_id, story_id=story_id, revision=revision)
    source_request, input_ref, source_output_sha256, eligible, order, coverage = _load_source_context(
        store, source_run_id
    )
    recorded = _load_recorded_responses(
        store, source_run_id=source_run_id, story_id=story_id, revision=revision
    )
    by_fingerprint = {rec.fingerprint: rec for rec in recorded}

    replayed: list[ReplayedCandidate] = []
    input_tokens = 0
    output_tokens = 0

    with offline_network_disabled():
        clusters = _cluster_sources(source_request, eligible, order, cluster)
        for key, sources, fingerprint in clusters:
            rec = by_fingerprint.get(fingerprint)
            if rec is None:
                continue
            enrichment = ScoutEnrichment.model_validate_json(rec.response_json)
            warnings = combine_warnings(enrichment.warnings, sources)
            candidate = StoryCandidate(
                **(enrichment.model_dump() | {"warnings": warnings}),
                schema_id=SCOUT_SCHEMA,
                kind="editorial",
                story_id=rec.story_id,
                cluster_id=rec.cluster_id,
                revision=rec.revision,
                fingerprint=fingerprint,
                representative=sources[0].ref,
                members=sources,
            )
            if not set(candidate.topics) <= set(source_request.topic_policy.topics):
                continue
            reproduced_sha = sha256(candidate.canonical_bytes()).hexdigest()
            replayed.append(
                ReplayedCandidate(
                    story_id=rec.story_id,
                    cluster_id=rec.cluster_id,
                    revision=rec.revision,
                    cluster_key=key,
                    fingerprint=fingerprint,
                    original_artifact_sha256=rec.candidate_sha256,
                    reproduced_artifact_sha256=reproduced_sha,
                    hash_matches=(reproduced_sha == rec.candidate_sha256),
                    candidate=candidate,
                    reported_tokens=rec.usage,
                )
            )
            input_tokens += rec.input_tokens
            output_tokens += rec.output_tokens

    if replayed:
        all_matched = all(bool(item.hash_matches) for item in replayed)
    else:
        all_matched = coverage.status == "empty" and not recorded

    config = source_request.configuration
    return ScoutAuditReplayOutput(
        source_run_id=source_request.run_id,
        source_invocation_id=source_request.invocation_id,
        source_input_sha256=input_ref.sha256,
        source_output_sha256=source_output_sha256,
        config_hash=config.record_hash(),
        prompt_hash=config.prompt_hash,
        feed_snapshot=config.feed_snapshot,
        cluster_version=config.cluster_version,
        provider=config.provider,
        model=config.model,
        replay_coverage=coverage,
        unreplayable_items=coverage.unreplayable_items,
        candidates=tuple(replayed),
        all_hashes_matched=all_matched,
        reported_tokens=ScoutTokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def reeval_scout(
    store: ArtifactStore,
    *,
    enrich: Enricher,
    source_run_id: UUID | None = None,
    story_id: UUID | None = None,
    revision: int | None = None,
    model: str | None = None,
    provider: str | None = None,
    prompt_version: str | None = None,
    prompt_hash: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    experiment_id: str | None = None,
    hard_spend_cap_usd: Decimal = Decimal("0.00"),
    run_id: UUID | None = None,
    invocation_id: UUID | None = None,
    telemetry: Telemetry | None = None,
    cluster: Clusterer = cluster_items,
    allow_network: bool = False,
) -> ScoutReevalOutput:
    """Run separately identified re-evaluation under a new run_id, lineage link, and experiment budget."""
    resolved_source_run_id = _resolve_source_run(store, run_id=source_run_id, story_id=story_id, revision=revision)
    source_request, source_input_ref, _, eligible, order, coverage = _load_source_context(
        store, resolved_source_run_id
    )
    recorded = _load_recorded_responses(
        store, source_run_id=resolved_source_run_id, story_id=story_id, revision=revision
    )
    by_fingerprint = {rec.fingerprint: rec for rec in recorded}

    new_run_id = run_id or uuid4()
    new_invocation_id = invocation_id or uuid4()
    if new_run_id == source_request.run_id or new_invocation_id == source_request.invocation_id:
        raise ValueError("reeval must use a distinct run_id and invocation_id")

    src_cfg = source_request.configuration
    new_cfg = src_cfg.model_copy(
        update={
            "model": model if model is not None else src_cfg.model,
            "provider": provider if provider is not None else src_cfg.provider,
            "prompt_version": prompt_version if prompt_version is not None else src_cfg.prompt_version,
            "prompt_hash": prompt_hash if prompt_hash is not None else src_cfg.prompt_hash,
            "temperature": temperature if temperature is not None else src_cfg.temperature,
        }
    )
    new_limits = source_request.limits.model_copy(
        update={
            "max_tokens": max_tokens if max_tokens is not None else source_request.limits.max_tokens,
        }
    )
    new_request = ScoutRunInput.model_validate(
        source_request.model_copy(
            update={
                "run_id": new_run_id,
                "invocation_id": new_invocation_id,
                "configuration": new_cfg,
                "limits": new_limits,
            }
        ).model_dump()
    )
    resolved_experiment_id = experiment_id or f"reeval-{new_run_id}"
    new_input_ref = _pin_request(store, new_request)
    SpendLedger(store.conn).ensure_experiment(
        resolved_experiment_id,
        hard_spend_cap_usd=hard_spend_cap_usd,
        config_hash=new_cfg.record_hash(),
    )

    @contextmanager
    def _noop_net() -> Iterator[None]:
        yield

    net_guard: Callable[[], Iterator[None]] = _noop_net if allow_network else offline_network_disabled  # type: ignore[assignment]

    def _execute(trace_id: str) -> tuple[list[ReplayedCandidate], int, int]:
        replayed: list[ReplayedCandidate] = []
        input_tokens = 0
        output_tokens = 0
        with net_guard():
            clusters = _cluster_sources(new_request, eligible, order, cluster)
            for key, sources, fingerprint in clusters:
                rec = by_fingerprint.get(fingerprint)
                if story_id is not None and rec is None:
                    continue
                if rec is not None:
                    c_story_id = rec.story_id
                    c_cluster_id = rec.cluster_id
                    c_revision = rec.revision
                    orig_sha = rec.candidate_sha256
                else:
                    c_story_id, c_cluster_id, latest_rev, latest_fp, _ = resolve_story_identity(
                        store.conn, sources, key, new_cfg.cluster_version, CLUSTER_NAMESPACE
                    )
                    c_revision = 1 if latest_rev == 0 else (latest_rev if latest_fp == fingerprint else latest_rev + 1)
                    orig_sha = None

                tokens_left = new_request.limits.max_tokens - input_tokens - output_tokens
                if tokens_left <= 0:
                    break
                result = enrich(
                    new_request,
                    sources,
                    max_tokens=tokens_left,
                    timeout_seconds=new_request.limits.timeout_seconds,
                )
                usage = ScoutTokenUsage.model_validate(result.usage.model_dump())
                if usage.total > tokens_left:
                    input_tokens += usage.input_tokens
                    output_tokens += usage.output_tokens
                    continue
                enrichment = ScoutEnrichment.model_validate_json(result.output)
                warnings = combine_warnings(enrichment.warnings, sources)
                candidate = StoryCandidate(
                    **(enrichment.model_dump() | {"warnings": warnings}),
                    schema_id=SCOUT_SCHEMA,
                    kind="editorial",
                    story_id=c_story_id,
                    cluster_id=c_cluster_id,
                    revision=c_revision,
                    fingerprint=fingerprint,
                    representative=sources[0].ref,
                    members=sources,
                )
                if not set(candidate.topics) <= set(new_request.topic_policy.topics):
                    input_tokens += usage.input_tokens
                    output_tokens += usage.output_tokens
                    continue
                cand_ref = _put_artifact(store, candidate.canonical_bytes(), "application/json", candidate.schema_version)
                record_replay_response(
                    store.conn,
                    run_id=new_run_id,
                    candidate=candidate,
                    cluster_key=key,
                    cluster_version=new_cfg.cluster_version,
                    response_bytes=result.output,
                    usage=usage,
                    candidate_sha256=cand_ref.sha256,
                )
                replayed.append(
                    ReplayedCandidate(
                        story_id=c_story_id,
                        cluster_id=c_cluster_id,
                        revision=c_revision,
                        cluster_key=key,
                        fingerprint=fingerprint,
                        original_artifact_sha256=orig_sha,
                        reproduced_artifact_sha256=cand_ref.sha256,
                        hash_matches=(cand_ref.sha256 == orig_sha) if orig_sha is not None else None,
                        candidate=candidate,
                        reported_tokens=usage,
                    )
                )
                input_tokens += usage.input_tokens
                output_tokens += usage.output_tokens
        return replayed, input_tokens, output_tokens

    if telemetry is not None:
        with telemetry.span(
            "agent.invoke",
            SCOUT_PROJECT,
            {
                "run.id": new_run_id,
                "invocation.id": new_invocation_id,
                "agent.name": "news_scout_reeval",
                "agent.version": "1.0",
                "replay.mode": "reeval",
                "source.run.id": str(source_request.run_id),
                "experiment.id": resolved_experiment_id,
                "input.ref": new_input_ref.sha256,
                "config.hash": new_cfg.record_hash(),
                "prompt.hash": new_cfg.prompt_hash,
                "model.provider": new_cfg.provider,
                "model.name": new_cfg.model,
            },
        ) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            record_run_lineage(
                store.conn,
                run_id=new_run_id,
                invocation_id=new_invocation_id,
                replay_mode="reeval",
                source_run_id=source_request.run_id,
                source_input_sha256=source_input_ref.sha256,
                input_sha256=new_input_ref.sha256,
                config_hash=new_cfg.record_hash(),
                prompt_hash=new_cfg.prompt_hash,
                provider=new_cfg.provider,
                model=new_cfg.model,
                experiment_id=resolved_experiment_id,
                trace_id=trace_id,
            )
            replayed, input_tokens, output_tokens = _execute(trace_id)
    else:
        trace_id = "0" * 32
        record_run_lineage(
            store.conn,
            run_id=new_run_id,
            invocation_id=new_invocation_id,
            replay_mode="reeval",
            source_run_id=source_request.run_id,
            source_input_sha256=source_input_ref.sha256,
            input_sha256=new_input_ref.sha256,
            config_hash=new_cfg.record_hash(),
            prompt_hash=new_cfg.prompt_hash,
            provider=new_cfg.provider,
            model=new_cfg.model,
            experiment_id=resolved_experiment_id,
            trace_id=trace_id,
        )
        replayed, input_tokens, output_tokens = _execute(trace_id)

    preliminary = ScoutReevalOutput(
        run_id=new_run_id,
        invocation_id=new_invocation_id,
        source_run_id=source_request.run_id,
        source_invocation_id=source_request.invocation_id,
        source_input_sha256=source_input_ref.sha256,
        input_sha256=new_input_ref.sha256,
        output_sha256=None,
        experiment_id=resolved_experiment_id,
        hard_spend_cap_usd=hard_spend_cap_usd,
        source_config_hash=src_cfg.record_hash(),
        source_prompt_hash=src_cfg.prompt_hash,
        source_model=src_cfg.model,
        config_hash=new_cfg.record_hash(),
        prompt_hash=new_cfg.prompt_hash,
        prompt_version=new_cfg.prompt_version,
        provider=new_cfg.provider,
        model=new_cfg.model,
        temperature=new_cfg.temperature,
        network_disabled=not allow_network,
        replay_coverage=coverage,
        unreplayable_items=coverage.unreplayable_items,
        candidates=tuple(replayed),
        reported_tokens=ScoutTokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        trace_id=trace_id,
    )
    out_ref = _put_artifact(store, preliminary.canonical_bytes(), "application/json", "1.0")
    outcome = "complete" if replayed else "empty"
    record_run_output(store.conn, run_id=new_run_id, output_sha256=out_ref.sha256, outcome=outcome)
    return preliminary.model_copy(update={"output_sha256": out_ref.sha256})
