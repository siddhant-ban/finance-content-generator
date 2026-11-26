"""Durable Scout delivery intent, pre-dispatch replay snapshots, and crash/overlap recovery."""
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import sqlite3
from typing import Iterator, Literal
from uuid import UUID

from pydantic import Field

from app.contracts.common import ArtifactRef, Nonempty, Sha256, StrictModel
from app.contracts.evidence import Revision
from app.contracts.scout import (
    ItemReason,
    ItemStatus,
    ScoutEnrichment,
    ScoutFeedItem,
    ScoutRunInput,
    ScoutRunOutput,
    ScoutTokenUsage,
    StoryCandidate,
)
from app.scout.workflow import deliver_candidate, record_story_revision, scout_state
from app.storage.artifacts import ArtifactStore
from app.workflow.state import PolicyDenied, StateConflict, StateService

DeliveryStatus = Literal["pending", "delivered", "failed"]
ReplayMode = Literal["live", "audit_replay", "reeval"]
InputRights = Literal["unknown", "metadata_only", "excerpt_permitted", "unparsed"]


def _now_iso(clock: Callable[[], datetime] | None = None) -> str:
    current = clock() if clock is not None else datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


@contextmanager
def _immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:
        raise ValueError("Scout delivery persistence requires an idle connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _artifact_ref(conn: sqlite3.Connection, digest: str) -> ArtifactRef:
    row = conn.execute(
        "SELECT sha256, media_type, size_bytes, schema_version FROM artifacts WHERE sha256 = ?",
        (digest,),
    ).fetchone()
    if row is None:
        raise ValueError(f"missing artifact metadata for {digest}")
    return ArtifactRef(sha256=row[0], media_type=row[1], size_bytes=row[2], schema_version=row[3])


class DeliveryRecord(StrictModel):
    """Persisted durable handoff intent and completion state for one (story_id, revision)."""

    story_id: UUID
    revision: Revision
    cluster_id: UUID
    artifact_sha256: Sha256
    run_id: UUID
    invocation_id: UUID
    status: DeliveryStatus
    replay_mode: ReplayMode = "live"
    source_run_id: UUID | None = None
    trace_id: str | None = None
    error_code: str | None = None
    created_at: Nonempty
    updated_at: Nonempty


class ReplayInputSnapshot(StrictModel):
    """Rights-aware frozen input snapshot persisted before clustering or dispatch."""

    item_order: int = Field(ge=0)
    item_id: Nonempty
    rights: InputRights
    retained: bool
    retention_reason: Nonempty
    source_id: UUID | None = None
    source_revision: Revision | None = None
    source_record_hash: Sha256 | None = None
    canonical_url: str | None = None
    payload_sha256: Sha256 | None = None
    payload_json: str | None = None

    @classmethod
    def from_feed_item(cls, *, item_order: int, item_id: str, item: ScoutFeedItem) -> "ReplayInputSnapshot":
        source = item.source
        if source.rights == "excerpt_permitted":
            canonical = item.canonical_bytes().decode("utf-8")
            return cls(
                item_order=item_order,
                item_id=item_id,
                rights="excerpt_permitted",
                retained=True,
                retention_reason="excerpt_permitted",
                source_id=source.source_id,
                source_revision=source.revision,
                source_record_hash=source.record_hash(),
                canonical_url=source.canonical_url,
                payload_sha256=sha256(canonical.encode("utf-8")).hexdigest(),
                payload_json=canonical,
            )
        reason = "metadata_only_rights" if source.rights == "metadata_only" else "unknown_rights"
        return cls(
            item_order=item_order,
            item_id=item_id,
            rights=source.rights,
            retained=False,
            retention_reason=reason,
            source_id=source.source_id,
            source_revision=source.revision,
            source_record_hash=source.record_hash(),
            canonical_url=source.canonical_url,
            payload_sha256=None,
            payload_json=None,
        )

    @classmethod
    def unretained(
        cls,
        *,
        item_order: int,
        item_id: str,
        rights: InputRights,
        retention_reason: str,
        source_id: UUID | None = None,
        source_revision: int | None = None,
        source_record_hash: str | None = None,
        canonical_url: str | None = None,
    ) -> "ReplayInputSnapshot":
        return cls(
            item_order=item_order,
            item_id=item_id,
            rights=rights,
            retained=False,
            retention_reason=retention_reason,
            source_id=source_id,
            source_revision=source_revision,
            source_record_hash=source_record_hash,
            canonical_url=canonical_url,
            payload_sha256=None,
            payload_json=None,
        )


class RecordedModelResponse(StrictModel):
    """Frozen validated model enrichment and cluster identity persisted before dispatch."""

    run_id: UUID
    story_id: UUID
    revision: Revision
    cluster_id: UUID
    cluster_key: Nonempty
    cluster_version: Nonempty
    fingerprint: Sha256
    response_sha256: Sha256
    response_json: Nonempty
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    candidate_sha256: Sha256
    created_at: Nonempty

    @property
    def usage(self) -> ScoutTokenUsage:
        return ScoutTokenUsage(input_tokens=self.input_tokens, output_tokens=self.output_tokens)


class DeliveryOutcome(StrictModel):
    """Result of durable intent-backed candidate delivery."""

    status: ItemStatus
    reason: ItemReason
    candidate_ref: ArtifactRef | None = None
    delivery: DeliveryRecord | None = None
    retry_with_next_revision: bool = False


def _row_to_delivery(row: tuple) -> DeliveryRecord:
    return DeliveryRecord(
        story_id=UUID(row[0]),
        revision=int(row[1]),
        cluster_id=UUID(row[2]),
        artifact_sha256=row[3],
        run_id=UUID(row[4]),
        invocation_id=UUID(row[5]),
        status=row[6],
        replay_mode=row[7],
        source_run_id=UUID(row[8]) if row[8] else None,
        trace_id=row[9],
        error_code=row[10],
        created_at=row[11],
        updated_at=row[12],
    )


_SELECT_DELIVERY = (
    "SELECT story_id, revision, cluster_id, artifact_sha256, run_id, invocation_id, "
    "status, replay_mode, source_run_id, trace_id, error_code, created_at, updated_at "
    "FROM scout_deliveries"
)


def get_deliveries(
    conn: sqlite3.Connection,
    *,
    run_id: UUID | None = None,
    story_id: UUID | None = None,
    status: DeliveryStatus | None = None,
) -> tuple[DeliveryRecord, ...]:
    """Query persisted delivery records ordered deterministically."""
    clauses: list[str] = []
    params: list[object] = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(str(run_id))
    if story_id is not None:
        clauses.append("story_id = ?")
        params.append(str(story_id))
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"{_SELECT_DELIVERY}{where} ORDER BY created_at ASC, story_id ASC, revision ASC",
        tuple(params),
    ).fetchall()
    return tuple(_row_to_delivery(row) for row in rows)


def record_run_lineage(
    conn: sqlite3.Connection,
    *,
    run_id: UUID,
    invocation_id: UUID,
    replay_mode: ReplayMode,
    source_run_id: UUID | None,
    source_input_sha256: str | None,
    input_sha256: str,
    config_hash: str,
    prompt_hash: str,
    provider: str,
    model: str,
    experiment_id: str | None,
    trace_id: str | None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Persist immutable run lineage before dispatch (idempotent for matching retries)."""
    timestamp = _now_iso(clock)
    with _immediate_tx(conn):
        conn.execute(
            "INSERT INTO scout_run_lineage("
            "run_id, invocation_id, replay_mode, source_run_id, source_input_sha256, "
            "input_sha256, config_hash, prompt_hash, provider, model, experiment_id, trace_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                str(run_id),
                str(invocation_id),
                replay_mode,
                str(source_run_id) if source_run_id else None,
                source_input_sha256,
                input_sha256,
                config_hash,
                prompt_hash,
                provider,
                model,
                experiment_id,
                trace_id,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT invocation_id, replay_mode, source_run_id, source_input_sha256, "
            "input_sha256, config_hash, prompt_hash, provider, model, experiment_id "
            "FROM scout_run_lineage WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        expected = (
            str(invocation_id),
            replay_mode,
            str(source_run_id) if source_run_id else None,
            source_input_sha256,
            input_sha256,
            config_hash,
            prompt_hash,
            provider,
            model,
            experiment_id,
        )
        if row is None or tuple(row) != expected:
            raise ValueError("Scout run lineage conflict for run_id")


def record_run_output(
    conn: sqlite3.Connection,
    *,
    run_id: UUID,
    output_sha256: str,
    outcome: str,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Persist immutable finalized run output reference."""
    timestamp = _now_iso(clock)
    with _immediate_tx(conn):
        conn.execute(
            "INSERT INTO scout_run_outputs(run_id, output_sha256, outcome, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (str(run_id), output_sha256, outcome, timestamp),
        )
        row = conn.execute(
            "SELECT output_sha256, outcome FROM scout_run_outputs WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if row is None or tuple(row) != (output_sha256, outcome):
            raise ValueError("Scout run output conflict for run_id")


def load_saved_run_output(store: ArtifactStore, run_id: UUID) -> ScoutRunOutput | None:
    """Load finalized ScoutRunOutput if this run_id already completed."""
    row = store.conn.execute(
        "SELECT output_sha256 FROM scout_run_outputs WHERE run_id = ?",
        (str(run_id),),
    ).fetchone()
    if row is None:
        return None
    ref = _artifact_ref(store.conn, row[0])
    return ScoutRunOutput.model_validate_json(store.read(ref))


def record_replay_inputs(
    conn: sqlite3.Connection,
    *,
    run_id: UUID,
    items: Sequence[ReplayInputSnapshot],
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Persist rights-checked input snapshots before clustering or dispatch."""
    timestamp = _now_iso(clock)
    with _immediate_tx(conn):
        for snap in items:
            conn.execute(
                "INSERT INTO scout_replay_inputs("
                "run_id, item_order, item_id, rights, retained, retention_reason, "
                "source_id, source_revision, source_record_hash, canonical_url, "
                "payload_sha256, payload_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    str(run_id),
                    snap.item_order,
                    snap.item_id,
                    snap.rights,
                    1 if snap.retained else 0,
                    snap.retention_reason,
                    str(snap.source_id) if snap.source_id else None,
                    snap.source_revision,
                    snap.source_record_hash,
                    snap.canonical_url,
                    snap.payload_sha256,
                    snap.payload_json,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT item_id, rights, retained, payload_sha256 "
                "FROM scout_replay_inputs WHERE run_id = ? AND item_order = ?",
                (str(run_id), snap.item_order),
            ).fetchone()
            expected = (snap.item_id, snap.rights, 1 if snap.retained else 0, snap.payload_sha256)
            if row is None or tuple(row) != expected:
                raise ValueError("Scout replay input snapshot conflict")


def _insert_replay_response_in_tx(
    conn: sqlite3.Connection,
    *,
    run_id: UUID,
    candidate: StoryCandidate,
    cluster_key: str,
    cluster_version: str,
    response_bytes: bytes,
    usage: ScoutTokenUsage,
    candidate_sha256: str,
    timestamp: str,
) -> None:
    enrichment = ScoutEnrichment.model_validate_json(response_bytes)
    canonical_response = enrichment.canonical_bytes().decode("utf-8")
    response_sha256 = sha256(canonical_response.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO scout_replay_responses("
        "run_id, story_id, revision, cluster_id, cluster_key, cluster_version, "
        "fingerprint, response_sha256, response_json, input_tokens, output_tokens, "
        "candidate_sha256, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            str(run_id),
            str(candidate.story_id),
            candidate.revision,
            str(candidate.cluster_id),
            cluster_key,
            cluster_version,
            candidate.fingerprint,
            response_sha256,
            canonical_response,
            usage.input_tokens,
            usage.output_tokens,
            candidate_sha256,
            timestamp,
        ),
    )
    row = conn.execute(
        "SELECT fingerprint, response_sha256, candidate_sha256 "
        "FROM scout_replay_responses WHERE run_id = ? AND story_id = ? AND revision = ?",
        (str(run_id), str(candidate.story_id), candidate.revision),
    ).fetchone()
    if row is None or tuple(row) != (candidate.fingerprint, response_sha256, candidate_sha256):
        raise ValueError("Scout replay response snapshot conflict")


def record_replay_response(
    conn: sqlite3.Connection,
    *,
    run_id: UUID,
    candidate: StoryCandidate,
    cluster_key: str,
    cluster_version: str,
    response_bytes: bytes,
    usage: ScoutTokenUsage,
    candidate_sha256: str,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Persist validated model response snapshot before dispatch."""
    timestamp = _now_iso(clock)
    with _immediate_tx(conn):
        _insert_replay_response_in_tx(
            conn,
            run_id=run_id,
            candidate=candidate,
            cluster_key=cluster_key,
            cluster_version=cluster_version,
            response_bytes=response_bytes,
            usage=usage,
            candidate_sha256=candidate_sha256,
            timestamp=timestamp,
        )


def load_replay_response(
    conn: sqlite3.Connection,
    *,
    run_id: UUID,
    story_id: UUID,
    revision: int,
) -> RecordedModelResponse | None:
    """Load an already-recorded pre-dispatch model response for a run retry."""
    row = conn.execute(
        "SELECT run_id, story_id, revision, cluster_id, cluster_key, cluster_version, "
        "fingerprint, response_sha256, response_json, input_tokens, output_tokens, "
        "candidate_sha256, created_at "
        "FROM scout_replay_responses WHERE run_id = ? AND story_id = ? AND revision = ?",
        (str(run_id), str(story_id), revision),
    ).fetchone()
    if row is None:
        return None
    return RecordedModelResponse(
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


def _load_delivery_context(
    store: ArtifactStore, row: DeliveryRecord
) -> tuple[StoryCandidate, ArtifactRef, str, str]:
    ref = _artifact_ref(store.conn, row.artifact_sha256)
    candidate = StoryCandidate.model_validate_json(store.read(ref))
    meta = store.conn.execute(
        "SELECT cluster_key, cluster_version FROM scout_replay_responses "
        "WHERE run_id = ? AND story_id = ? AND revision = ?",
        (str(row.run_id), str(row.story_id), row.revision),
    ).fetchone()
    if meta is not None:
        cluster_key, cluster_version = meta[0], meta[1]
    else:
        req_row = store.conn.execute(
            "SELECT input_sha256 FROM scout_run_requests WHERE run_id = ?",
            (str(row.run_id),),
        ).fetchone()
        if req_row is None:
            raise ValueError("missing replay response context for pending delivery")
        req_ref = _artifact_ref(store.conn, req_row[0])
        req = ScoutRunInput.model_validate_json(store.read(req_ref))
        cluster_key = candidate.representative_member.source.canonical_url
        cluster_version = req.configuration.cluster_version
    return candidate, ref, cluster_key, cluster_version


def _ensure_revision_recorded(
    store: ArtifactStore,
    *,
    candidate: StoryCandidate,
    ref: ArtifactRef,
    run_id: UUID,
    cluster_key: str,
    cluster_version: str,
) -> None:
    record_story_revision(
        store,
        candidate=candidate,
        ref=ref,
        run_id=run_id,
        cluster_key=cluster_key,
        cluster_version=cluster_version,
        reason_code="candidate_created" if candidate.revision == 1 else "source_correction",
    )


def _complete_pending_row(
    store: ArtifactStore,
    workflow: StateService,
    *,
    row: DeliveryRecord,
    candidate: StoryCandidate,
    ref: ArtifactRef,
    cluster_key: str,
    cluster_version: str,
    clock: Callable[[], datetime] | None = None,
) -> DeliveryRecord:
    trace_id = row.trace_id or ("0" * 32)
    try:
        deliver_candidate(workflow, candidate, ref, row.run_id, trace_id)
        _ensure_revision_recorded(
            store,
            candidate=candidate,
            ref=ref,
            run_id=row.run_id,
            cluster_key=cluster_key,
            cluster_version=cluster_version,
        )
    except (StateConflict, PolicyDenied):
        now = _now_iso(clock)
        with _immediate_tx(store.conn):
            store.conn.execute(
                "UPDATE scout_deliveries SET status = 'failed', error_code = 'revision_conflict', updated_at = ? "
                "WHERE story_id = ? AND revision = ? AND status = 'pending'",
                (now, str(row.story_id), row.revision),
            )
            updated = store.conn.execute(
                f"{_SELECT_DELIVERY} WHERE story_id = ? AND revision = ?",
                (str(row.story_id), row.revision),
            ).fetchone()
            assert updated is not None
            return _row_to_delivery(updated)

    now = _now_iso(clock)
    with _immediate_tx(store.conn):
        store.conn.execute(
            "UPDATE scout_deliveries SET status = 'delivered', error_code = NULL, updated_at = ? "
            "WHERE story_id = ? AND revision = ? AND status = 'pending'",
            (now, str(row.story_id), row.revision),
        )
        updated = store.conn.execute(
            f"{_SELECT_DELIVERY} WHERE story_id = ? AND revision = ?",
            (str(row.story_id), row.revision),
        ).fetchone()
        assert updated is not None
        return _row_to_delivery(updated)


def deliver_story_candidate(
    store: ArtifactStore,
    workflow: StateService,
    *,
    request: ScoutRunInput,
    candidate: StoryCandidate,
    ref: ArtifactRef,
    cluster_key: str,
    trace_id: str,
    response_bytes: bytes,
    usage: ScoutTokenUsage,
    clock: Callable[[], datetime] | None = None,
) -> DeliveryOutcome:
    """Persist response snapshot & pending intent before dispatch; ensure single logical handoff."""
    now = _now_iso(clock)
    with _immediate_tx(store.conn):
        store.conn.execute(
            "INSERT INTO scout_deliveries("
            "story_id, revision, cluster_id, artifact_sha256, run_id, invocation_id, "
            "status, replay_mode, source_run_id, trace_id, error_code, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 'pending', 'live', NULL, ?, NULL, ?, ?) "
            "ON CONFLICT(story_id, revision) DO NOTHING",
            (
                str(candidate.story_id),
                candidate.revision,
                str(candidate.cluster_id),
                ref.sha256,
                str(request.run_id),
                str(request.invocation_id),
                trace_id,
                now,
                now,
            ),
        )
        raw = store.conn.execute(
            f"{_SELECT_DELIVERY} WHERE story_id = ? AND revision = ?",
            (str(candidate.story_id), candidate.revision),
        ).fetchone()
        assert raw is not None
        row = _row_to_delivery(raw)
        if row.run_id == request.run_id:
            _insert_replay_response_in_tx(
                store.conn,
                run_id=request.run_id,
                candidate=candidate,
                cluster_key=cluster_key,
                cluster_version=request.configuration.cluster_version,
                response_bytes=response_bytes,
                usage=usage,
                candidate_sha256=ref.sha256,
                timestamp=now,
            )

    if row.run_id == request.run_id:
        pinned_ref = _artifact_ref(store.conn, row.artifact_sha256)
        pinned_candidate = (
            candidate
            if pinned_ref == ref
            else StoryCandidate.model_validate_json(store.read(pinned_ref))
        )
        if row.status == "delivered":
            _ensure_revision_recorded(
                store,
                candidate=pinned_candidate,
                ref=pinned_ref,
                run_id=row.run_id,
                cluster_key=cluster_key,
                cluster_version=request.configuration.cluster_version,
            )
            status: ItemStatus = "new" if pinned_candidate.revision == 1 else "updated"
            return DeliveryOutcome(status=status, reason="candidate_created", candidate_ref=pinned_ref, delivery=row)
        if row.status == "failed":
            return DeliveryOutcome(
                status="failed",
                reason="revision_conflict",
                candidate_ref=None,
                delivery=row,
            )

        updated = _complete_pending_row(
            store,
            workflow,
            row=row,
            candidate=pinned_candidate,
            ref=pinned_ref,
            cluster_key=cluster_key,
            cluster_version=request.configuration.cluster_version,
            clock=clock,
        )
        if updated.status == "failed":
            return DeliveryOutcome(status="failed", reason="revision_conflict", candidate_ref=None, delivery=updated)
        status = "new" if pinned_candidate.revision == 1 else "updated"
        return DeliveryOutcome(status=status, reason="candidate_created", candidate_ref=pinned_ref, delivery=updated)

    # Another invocation already claimed (story_id, revision).
    existing_candidate, existing_ref, existing_key, existing_version = _load_delivery_context(store, row)
    if row.status == "pending":
        row = _complete_pending_row(
            store,
            workflow,
            row=row,
            candidate=existing_candidate,
            ref=existing_ref,
            cluster_key=existing_key,
            cluster_version=existing_version,
            clock=clock,
        )

    if row.status == "failed":
        return DeliveryOutcome(status="failed", reason="revision_conflict", candidate_ref=None, delivery=row)

    if existing_candidate.fingerprint != candidate.fingerprint:
        return DeliveryOutcome(
            status="unchanged",
            reason="already_delivered",
            candidate_ref=None,
            delivery=row,
            retry_with_next_revision=True,
        )

    return DeliveryOutcome(
        status="unchanged",
        reason="already_delivered",
        candidate_ref=existing_ref,
        delivery=row,
    )


def recover_pending_deliveries(
    store: ArtifactStore,
    *,
    workflow: StateService | None = None,
    run_id: UUID | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[DeliveryRecord, ...]:
    """Resume any pending durable Scout deliveries to completion or explicit conflict failure."""
    pending = get_deliveries(store.conn, run_id=run_id, status="pending")
    if not pending:
        return ()
    state_service = workflow if workflow is not None else scout_state(store)
    recovered: list[DeliveryRecord] = []
    for row in pending:
        candidate, ref, cluster_key, cluster_version = _load_delivery_context(store, row)
        updated = _complete_pending_row(
            store,
            state_service,
            row=row,
            candidate=candidate,
            ref=ref,
            cluster_key=cluster_key,
            cluster_version=cluster_version,
            clock=clock,
        )
        recovered.append(updated)
    return tuple(recovered)
