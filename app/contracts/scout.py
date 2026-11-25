"""Version 1 discovery and Novelty handoff contracts."""
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StringConstraints, model_validator

from app.contracts.common import ArtifactRef, Nonempty, Sha256, StrictModel, UtcDatetime
from app.contracts.evidence import EvidenceRef, Revision, SourceRecord

SCOUT_SCHEMA = "scout.candidate.v1"
SCOUT_PROJECT = "finance-media-news-scout"
Topic = Literal["prices", "jobs", "borrowing"]
Warning = Literal["seasonally_adjusted", "conflicting_sources", "revision_possible", "limited_coverage"]
Headline = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
StatementText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
TraceId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class ScoutTopicPolicy(StrictModel):
    version: Nonempty
    topics: Annotated[tuple[Topic, ...], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def unique_topics(self):
        if len(set(self.topics)) != len(self.topics):
            raise ValueError("topics must be unique")
        return self


class ScoutFingerprintSnapshot(StrictModel):
    version: Nonempty
    known: tuple[Sha256, ...]


class ScoutConfiguration(StrictModel):
    version: Nonempty
    feed_snapshot: Sha256
    cluster_version: Nonempty
    prompt_version: Nonempty
    prompt_hash: Sha256
    provider: Nonempty
    model: Nonempty
    temperature: Annotated[float, Field(ge=0, le=2)]


class ScoutLimits(StrictModel):
    max_items: Annotated[int, Field(ge=1, le=1000)]
    max_tokens: Annotated[int, Field(ge=0, le=1_000_000)]
    timeout_seconds: Annotated[float, Field(gt=0, le=3600)]


class ScoutRunInput(StrictModel):
    schema_version: Literal["1.0"]
    run_id: UUID
    invocation_id: UUID
    window_start: UtcDatetime
    window_end: UtcDatetime
    topic_policy: ScoutTopicPolicy
    fingerprints: ScoutFingerprintSnapshot
    configuration: ScoutConfiguration
    release_id: UUID
    limits: ScoutLimits

    @model_validator(mode="after")
    def ordered_window(self):
        if self.window_start >= self.window_end:
            raise ValueError("Scout window must have a positive duration")
        return self


def source_ref(source: SourceRecord) -> EvidenceRef:
    return EvidenceRef(source_id=source.source_id, source_revision=source.revision,
                       source_record_hash=source.record_hash())


class ScoutFeedItem(StrictModel):
    headline: Headline
    source: SourceRecord


class ScoutMember(ScoutFeedItem):
    ref: EvidenceRef

    @model_validator(mode="after")
    def complete_provenance(self):
        source = self.source
        if self.ref != source_ref(source):
            raise ValueError("member does not match its source pin")
        if source.published_at is None or source.published_at > source.retrieved_at:
            raise ValueError("candidate requires valid publication and retrieval times")
        if source.rights == "unknown" or not source.publisher.strip():
            raise ValueError("candidate requires publisher and known retention rights")
        if not ((source.locator or "").strip() or (source.excerpt or "").strip()):
            raise ValueError("candidate requires a locator or permitted excerpt")
        return self


class AttributedStatement(StrictModel):
    text: StatementText
    sources: Annotated[tuple[EvidenceRef, ...], Field(min_length=1, max_length=1000)]


class ScoutEnrichment(StrictModel):
    schema_version: Literal["1.0"]
    summary: Annotated[tuple[AttributedStatement, ...], Field(min_length=1, max_length=12)]
    topics: Annotated[tuple[Topic, ...], Field(min_length=1, max_length=3)]
    entities: Annotated[tuple[Headline, ...], Field(max_length=30)]
    warnings: Annotated[tuple[Warning, ...], Field(max_length=4)]
    uncertainty: Annotated[tuple[AttributedStatement, ...], Field(max_length=12)] = ()


def candidate_fingerprint(members: tuple[ScoutMember, ...]) -> str:
    return sha256(b"\n".join(sorted(member.canonical_bytes() for member in members))).hexdigest()


class StoryCandidate(ScoutEnrichment):
    """The immutable artifact read directly by the Novelty consumer."""

    schema_id: Literal["scout.candidate.v1"]
    kind: Literal["editorial"]
    story_id: UUID
    cluster_id: UUID
    revision: Revision
    fingerprint: Sha256
    representative: EvidenceRef
    members: Annotated[tuple[ScoutMember, ...], Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def pinned_provenance(self):
        pins = {member.ref.canonical_bytes() for member in self.members}
        identities = {(member.ref.source_id, member.ref.source_revision) for member in self.members}
        if len(identities) != len(self.members):
            raise ValueError("duplicate member identity or conflicting revision pin")
        if self.representative.canonical_bytes() not in pins:
            raise ValueError("representative must be a member")
        if any(ref.canonical_bytes() not in pins
               for statement in self.summary + self.uncertainty for ref in statement.sources):
            raise ValueError("attribution must match a pinned member")
        if self.story_id != uuid5(self.cluster_id, "story"):
            raise ValueError("story identity must match its cluster")
        if self.fingerprint != candidate_fingerprint(self.members):
            raise ValueError("fingerprint must match the pinned members")
        return self

    @property
    def representative_member(self) -> ScoutMember:
        return next(member for member in self.members if member.ref == self.representative)


ItemStatus = Literal["new", "updated", "unchanged", "withheld", "failed", "unprocessed"]
ItemReason = Literal[
    "candidate_created", "known_fingerprint", "already_delivered", "no_publication_time",
    "outside_window", "unknown_rights", "invalid_provenance", "outside_topic_policy",
    "invalid_enrichment", "model_unavailable", "model_token_overrun", "cluster_failed",
    "revision_conflict", "item_limit", "token_limit", "time_limit",
]
RunIssue = Literal["feed_unavailable", "invalid_feed_batch", "cluster_failed", "time_limit"]


class ScoutItemDisposition(StrictModel):
    item_id: Nonempty
    status: ItemStatus
    reason: ItemReason
    candidate_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def candidate_matches_status(self):
        if self.status in {"new", "updated"} and self.candidate_ref is None:
            raise ValueError("accepted item needs a candidate reference")
        if self.status in {"withheld", "failed", "unprocessed"} and self.candidate_ref is not None:
            raise ValueError("unaccepted item cannot have a candidate reference")
        return self


class ScoutTokenUsage(StrictModel):
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def run_outcome(items: tuple[ScoutItemDisposition, ...], issue: RunIssue | None) -> Literal[
    "complete", "empty", "partial", "failed"
]:
    statuses = {item.status for item in items}
    if statuses & {"failed", "unprocessed"} or issue is not None:
        if statuses - {"failed"} or issue == "time_limit":
            return "partial"
        return "failed"
    return "complete" if any(item.candidate_ref for item in items) else "empty"


class ScoutRunOutput(StrictModel):
    schema_version: Literal["1.0"]
    run_id: UUID
    invocation_id: UUID
    input_ref: ArtifactRef
    outcome: Literal["complete", "empty", "partial", "failed"]
    trace_id: TraceId
    candidates: tuple[ArtifactRef, ...]
    items: tuple[ScoutItemDisposition, ...]
    reported_tokens: ScoutTokenUsage
    issue: RunIssue | None = None

    @model_validator(mode="after")
    def accounted_items(self):
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("each item needs exactly one disposition")
        reported = {item.candidate_ref.canonical_bytes() for item in self.items if item.candidate_ref}
        candidates = {ref.canonical_bytes() for ref in self.candidates}
        if reported != candidates or len(candidates) != len(self.candidates):
            raise ValueError("candidate list must match the ingestion report")
        if self.outcome != run_outcome(self.items, self.issue):
            raise ValueError("outcome must match the ingestion report")
        return self
