"""Versioned restricted release contracts; paths reference local private evidence."""
from typing import Annotated, Literal
from pydantic import Field
from app.contracts.common import Nonempty, Sha256, StrictModel, UtcDatetime
from app.evals.models import Split

class ReleasePolicy(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    policy_id: Literal['foundation.release.v1'] = 'foundation.release.v1'
    dataset_kind: Literal['synthetic_foundation'] = 'synthetic_foundation'
    required_splits: tuple[Split,...] = ('dev','holdout','prod_regression')
    deterministic_pass_rate: Literal[1.0] = 1.0
    rubric_pass_rate: Literal[1.0] = 1.0
    critical_false_accepts: Literal[0] = 0

class EvidencePin(StrictModel):
    path: Nonempty
    bundle_hash: Sha256

class ReleaseCandidate(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    agent_name: Literal['foundation'] = 'foundation'
    policy: ReleasePolicy = ReleasePolicy()
    evidence: dict[Split,EvidencePin]

class GateDecision(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    candidate_hash: Sha256
    status: Literal['APPROVED','ELIGIBLE','REQUIRES_BOOTSTRAP_APPROVAL','BLOCKED','ERROR']
    reasons: tuple[Nonempty,...] = ()
    baseline_hash: Sha256 | None = None
    current_version: Annotated[int,Field(ge=0)] = 0
    synthetic_only: Literal[True] = True

class ReleaseApproval(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    approval_id: Nonempty
    agent_name: Literal['foundation'] = 'foundation'
    candidate_hash: Sha256
    release_pins: dict[Nonempty,Nonempty]
    experiment_hashes: dict[Split,Sha256]
    baseline_hash: Sha256 | None
    policy_hash: Sha256
    version: Annotated[int,Field(ge=1)]
    bootstrap: bool
    approved_at: UtcDatetime
    synthetic_only: Literal[True] = True

class FeedbackAnnotation(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    annotation_ref: Nonempty
    annotation_hash: Sha256
    agent_name: Nonempty
    dataset_hash: Sha256
    privacy_review: Literal['PENDING','CLEARED','REJECTED'] = 'PENDING'
    rights_review: Literal['PENDING','CLEARED','REJECTED'] = 'PENDING'
    adjudication_review: Literal['PENDING','CLEARED','REJECTED'] = 'PENDING'

class FeedbackCandidate(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    feedback_id: Sha256
    status: Literal['QUARANTINED'] = 'QUARANTINED'
    annotation: FeedbackAnnotation
    queued_at: UtcDatetime
