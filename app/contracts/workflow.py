"""Shared state and publication manifest contracts, without transition authority."""
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.contracts.common import ArtifactRef, Nonempty, Sha256, StrictModel

class StoryState(StrEnum):
    DISCOVERED="DISCOVERED"
    CLUSTERED="CLUSTERED"
    SCORED="SCORED"
    RESEARCHED="RESEARCHED"
    VERIFIED="VERIFIED"
    SCRIPTED="SCRIPTED"
    RENDERED="RENDERED"
    AWAITING_APPROVAL="AWAITING_APPROVAL"
    APPROVED_FOR_PUBLISH="APPROVED_FOR_PUBLISH"
    PUBLISHED="PUBLISHED"
    ANALYZED="ANALYZED"
    NEEDS_RESEARCH="NEEDS_RESEARCH"
    NEEDS_REVISION="NEEDS_REVISION"
    REJECTED="REJECTED"

class ChangeKind(StrEnum):
    EVIDENCE="EVIDENCE"
    TEXT="TEXT"
    VISUAL="VISUAL"
    REVIEW_METADATA="REVIEW_METADATA"

class RevisionRef(StrictModel):
    record_id: UUID
    revision: Annotated[int,Field(ge=1)]
    sha256: Sha256

class PolicyCheck(StrictModel):
    code: Nonempty
    passed: bool

class PolicyReport(StrictModel):
    ready: bool
    checks: tuple[PolicyCheck,...]
    critical_failures: tuple[str,...] = ()
    evidence_refs: tuple[RevisionRef,...] = ()

    @model_validator(mode="after")
    def readiness_consistent(self):
        expected=bool(self.checks) and all(check.passed for check in self.checks) and not self.critical_failures
        if self.ready != expected:
            raise ValueError("readiness must match nonempty checks and critical failures")
        return self

class ReviewManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    story_id: UUID
    revision: Annotated[int,Field(ge=1)]
    kind: Literal["synthetic_foundation","editorial"]
    script: ArtifactRef
    dossier: ArtifactRef
    verification: ArtifactRef
    render: ArtifactRef
    subtitles: ArtifactRef
    captions: ArtifactRef
    charts: ArtifactRef
    attributions: ArtifactRef
    asset_register: ArtifactRef
    claim_revision_refs: tuple[RevisionRef,...] = ()
    source_revision_refs: tuple[RevisionRef,...] = ()
    internal_label: str | None = None
