"""Register only Scout's editorial discovery checkpoints."""
import json
from uuid import UUID

from app.contracts.common import ArtifactRef
from app.contracts.scout import SCOUT_SCHEMA, StoryCandidate
from app.contracts.workflow import PolicyCheck, StoryState
from app.storage.artifacts import ArtifactStore
from app.workflow.policy import ArtifactValidation, PolicyEngine, report
from app.workflow.state import StateConflict, StateService, TransitionRequest

SCOPE = "news_scout"


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
        )

    for stage in ("candidate", "cluster"):
        policy.register(stage, SCOUT_SCHEMA, "editorial", validate)


def deliver_candidate(
    workflow: StateService, candidate: StoryCandidate, ref: ArtifactRef, run_id: UUID, trace_id: str
) -> bool:
    """Finish the initial delivery and report whether it was already clustered."""
    key = ref.sha256
    try:
        workflow.create(candidate.story_id, run_id, ref, SCOPE, key)
    except StateConflict:
        if workflow.get(candidate.story_id).artifacts.get("candidate") != ref:
            raise StateConflict("Scout story already exists with a different candidate") from None

    snapshot = workflow.get(candidate.story_id)
    if snapshot.artifacts.get("cluster") == ref:
        return False
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
        return False
    return True
