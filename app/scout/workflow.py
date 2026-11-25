"""Register Scout's discovery checkpoints, revision history, and rework delivery."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from typing import Literal
from uuid import UUID, uuid5

from app.contracts.common import ArtifactRef
from app.contracts.scout import SCOUT_SCHEMA, ScoutMember, StoryCandidate
from app.contracts.workflow import PolicyCheck, StoryState
from app.storage.artifacts import ArtifactStore
from app.workflow.policy import ArtifactValidation, PolicyEngine, report
from app.workflow.state import StateConflict, StateService, TransitionRequest

SCOPE = "news_scout"
DeliveryStatus = Literal["new", "updated", "unchanged"]


@dataclass(frozen=True)
class StoryRevisionRecord:
    story_id: UUID
    cluster_id: UUID
    revision: int
    run_id: UUID
    cluster_key: str
    cluster_version: str
    fingerprint: str
    artifact_ref: ArtifactRef
    reason_code: str
    created_at: datetime
    candidate: StoryCandidate


def scout_state(store: ArtifactStore) -> StateService:
    policy = PolicyEngine(store)
    register_scout_policy(policy)
    return StateService(store.conn, store, policy)


def register_scout_policy(policy: PolicyEngine) -> None:
    def validate(data: dict) -> ArtifactValidation:
        candidate = StoryCandidate.model_validate_json(json.dumps(data))
        return ArtifactValidation(
            report=report([PolicyCheck(code="scout_candidate_schema", passed=True)]),
            discovery_story_id=candidate.story_id,
            discovery_revision=candidate.revision,
        )

    for stage in ("candidate", "cluster"):
        policy.register(stage, SCOUT_SCHEMA, "editorial", validate)


def resolve_story_identity(
    conn: sqlite3.Connection,
    sources: tuple[ScoutMember, ...],
    cluster_key: str,
    cluster_version: str,
    cluster_namespace: UUID,
) -> tuple[UUID, UUID, int, str | None, bool]:
    """Resolve stable story/cluster IDs and latest recorded revision for a cluster."""
    cluster_id = uuid5(cluster_namespace, cluster_version + "\n" + cluster_key)
    story_id = uuid5(cluster_id, "story")

    latest = conn.execute(
        "SELECT cluster_id, revision, fingerprint FROM scout_story_revisions "
        "WHERE story_id = ? ORDER BY revision DESC LIMIT 1",
        (str(story_id),),
    ).fetchone()

    if latest is None and sources:
        source_ids = [str(m.ref.source_id) for m in sources]
        urls = [m.source.canonical_url for m in sources]
        id_marks = ",".join("?" for _ in source_ids)
        url_marks = ",".join("?" for _ in urls)
        matches = conn.execute(
            f"SELECT DISTINCT r.story_id, r.cluster_id FROM scout_cluster_members m "
            f"JOIN scout_story_revisions r ON r.story_id = m.story_id AND r.revision = m.revision "
            f"WHERE m.source_id IN ({id_marks}) OR m.canonical_url IN ({url_marks})",
            (*source_ids, *urls),
        ).fetchall()
        if len(matches) > 1:
            return story_id, cluster_id, 0, None, True
        if len(matches) == 1:
            story_id = UUID(matches[0][0])
            cluster_id = UUID(matches[0][1])
            latest = conn.execute(
                "SELECT cluster_id, revision, fingerprint FROM scout_story_revisions "
                "WHERE story_id = ? ORDER BY revision DESC LIMIT 1",
                (str(story_id),),
            ).fetchone()

    if latest is None:
        return story_id, cluster_id, 0, None, False

    prior_revs = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT source_id, MAX(source_revision) FROM scout_cluster_members "
            "WHERE story_id = ? GROUP BY source_id",
            (str(story_id),),
        ).fetchall()
    }
    for member in sources:
        max_prior = prior_revs.get(str(member.ref.source_id))
        if max_prior is not None and member.ref.source_revision < max_prior:
            return story_id, UUID(latest[0]), int(latest[1]), latest[2], True

    return story_id, UUID(latest[0]), int(latest[1]), latest[2], False


def record_story_revision(
    store: ArtifactStore,
    *,
    candidate: StoryCandidate,
    ref: ArtifactRef,
    run_id: UUID,
    cluster_key: str,
    cluster_version: str,
    reason_code: str,
    created_at: datetime | None = None,
) -> None:
    """Persist an immutable revision and pinned source membership snapshot."""
    if store.conn.in_transaction:
        raise ValueError("Scout revision persistence requires an idle connection")
    timestamp = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with store.conn:
        store.conn.execute(
            "INSERT INTO scout_story_revisions("
            "story_id, cluster_id, revision, run_id, cluster_key, cluster_version, "
            "fingerprint, artifact_sha256, reason_code, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                str(candidate.story_id), str(candidate.cluster_id), candidate.revision,
                str(run_id), cluster_key, cluster_version,
                candidate.fingerprint, ref.sha256, reason_code, timestamp,
            ),
        )
        row = store.conn.execute(
            "SELECT cluster_id, fingerprint, artifact_sha256 FROM scout_story_revisions "
            "WHERE story_id = ? AND revision = ?",
            (str(candidate.story_id), candidate.revision),
        ).fetchone()
        if row is None or tuple(row) != (str(candidate.cluster_id), candidate.fingerprint, ref.sha256):
            raise StateConflict("Scout story revision already exists with a different artifact")

        for member in candidate.members:
            store.conn.execute(
                "INSERT INTO scout_cluster_members("
                "story_id, revision, source_id, source_revision, source_record_hash, canonical_url"
                ") VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    str(candidate.story_id), candidate.revision,
                    str(member.ref.source_id), member.ref.source_revision,
                    member.ref.source_record_hash, member.source.canonical_url,
                ),
            )
            member_row = store.conn.execute(
                "SELECT source_record_hash, canonical_url FROM scout_cluster_members "
                "WHERE story_id = ? AND revision = ? AND source_id = ? AND source_revision = ?",
                (
                    str(candidate.story_id), candidate.revision,
                    str(member.ref.source_id), member.ref.source_revision,
                ),
            ).fetchone()
            if member_row is None or tuple(member_row) != (member.ref.source_record_hash, member.source.canonical_url):
                raise StateConflict("Scout cluster member pin conflicts with recorded history")


def story_revisions(store: ArtifactStore, story_id: UUID) -> tuple[StoryRevisionRecord, ...]:
    """Load all persisted immutable revisions for a story in revision order."""
    rows = store.conn.execute(
        "SELECT r.story_id, r.cluster_id, r.revision, r.run_id, r.cluster_key, r.cluster_version, "
        "r.fingerprint, r.reason_code, r.created_at, "
        "a.sha256, a.media_type, a.size_bytes, a.schema_version "
        "FROM scout_story_revisions r "
        "JOIN artifacts a ON a.sha256 = r.artifact_sha256 "
        "WHERE r.story_id = ? ORDER BY r.revision ASC",
        (str(story_id),),
    ).fetchall()
    return tuple(_row_to_revision(store, row) for row in rows)


def all_story_revisions(store: ArtifactStore) -> tuple[StoryRevisionRecord, ...]:
    """Load all persisted story revisions across the store."""
    rows = store.conn.execute(
        "SELECT r.story_id, r.cluster_id, r.revision, r.run_id, r.cluster_key, r.cluster_version, "
        "r.fingerprint, r.reason_code, r.created_at, "
        "a.sha256, a.media_type, a.size_bytes, a.schema_version "
        "FROM scout_story_revisions r "
        "JOIN artifacts a ON a.sha256 = r.artifact_sha256 "
        "ORDER BY r.story_id ASC, r.revision ASC",
    ).fetchall()
    return tuple(_row_to_revision(store, row) for row in rows)


def _row_to_revision(store: ArtifactStore, row: tuple) -> StoryRevisionRecord:
    ref = ArtifactRef(sha256=row[9], media_type=row[10], size_bytes=row[11], schema_version=row[12])
    candidate = StoryCandidate.model_validate_json(store.read(ref))
    return StoryRevisionRecord(
        story_id=UUID(row[0]),
        cluster_id=UUID(row[1]),
        revision=int(row[2]),
        run_id=UUID(row[3]),
        cluster_key=row[4],
        cluster_version=row[5],
        fingerprint=row[6],
        artifact_ref=ref,
        reason_code=row[7],
        created_at=datetime.fromisoformat(row[8]),
        candidate=candidate,
    )


def deliver_candidate(
    workflow: StateService, candidate: StoryCandidate, ref: ArtifactRef, run_id: UUID, trace_id: str
) -> DeliveryStatus:
    """Deliver revision 1 via DISCOVERED->CLUSTERED or rework via CLUSTERED->NEEDS_REVISION->CLUSTERED."""
    key = ref.sha256
    if candidate.revision == 1:
        try:
            workflow.create(candidate.story_id, run_id, ref, SCOPE, key)
        except StateConflict:
            if workflow.get(candidate.story_id).artifacts.get("candidate") != ref:
                raise StateConflict("Scout story already exists with a different candidate") from None

        snapshot = workflow.get(candidate.story_id)
        if snapshot.artifacts.get("cluster") == ref:
            return "unchanged"
        # An incomplete delivery belongs to the run that discovered the story.
        if (snapshot.state != StoryState.DISCOVERED or snapshot.artifacts.get("candidate") != ref
                or snapshot.run_id != run_id):
            raise StateConflict("Scout story cannot finish its initial handoff")

        try:
            workflow.transition(TransitionRequest(
                story_id=candidate.story_id, target=StoryState.CLUSTERED, expected_version=snapshot.version,
                caller_scope=SCOPE, idempotency_key=key, artifact=ref, trace_id=trace_id,
            ))
        except StateConflict:
            if workflow.get(candidate.story_id).artifacts.get("cluster") != ref:
                raise
            return "unchanged"
        return "new"

    snapshot = workflow.get(candidate.story_id)
    if snapshot.artifacts.get("cluster") == ref:
        return "unchanged"

    if snapshot.state == StoryState.CLUSTERED:
        prior_ref = snapshot.artifacts.get("cluster")
        if prior_ref is None:
            raise StateConflict("Scout story missing prior cluster artifact")
        prior = StoryCandidate.model_validate_json(workflow.store.read(prior_ref))
        if candidate.revision != prior.revision + 1:
            raise StateConflict("Scout story revision must increment sequentially")
        snapshot = workflow.transition(TransitionRequest(
            story_id=candidate.story_id,
            target=StoryState.NEEDS_REVISION,
            expected_version=snapshot.version,
            caller_scope=SCOPE,
            idempotency_key=f"{key}:needs_revision",
            reason_code="source_correction",
            trace_id=trace_id,
        ))

    if snapshot.state == StoryState.NEEDS_REVISION and snapshot.resume_target == StoryState.CLUSTERED:
        try:
            workflow.transition(TransitionRequest(
                story_id=candidate.story_id,
                target=StoryState.CLUSTERED,
                expected_version=snapshot.version,
                caller_scope=SCOPE,
                idempotency_key=f"{key}:clustered",
                artifact=ref,
                reason_code="source_correction",
                trace_id=trace_id,
            ))
        except StateConflict:
            if workflow.get(candidate.story_id).artifacts.get("cluster") != ref:
                raise
            return "unchanged"
        return "updated"

    raise StateConflict("Scout story cannot accept correction in its current state")
