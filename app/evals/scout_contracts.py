"""Versioned serialized inputs for the Scout evaluator, independent of the worker."""

from typing import Annotated, Literal

from pydantic import Field, HttpUrl, JsonValue, model_validator

from app.contracts.common import Nonempty, Sha256, StrictModel, UtcDatetime
from app.evals.models import Adjudication, Rights, Split

VERSION = "1.0.0"
Topic = Literal["prices", "jobs", "borrowing"]
Warning = Literal["correction", "conflict", "uncertainty"]
Disposition = Literal["candidate", "undated", "inaccessible", "stale"]
Dimension = Literal["audience_relevance", "summary_fidelity", "entity_extraction", "uncertainty"]
Metric = Literal[
    "audience_relevance", "summary_fidelity", "entity_extraction", "uncertainty",
    "duplicate_precision", "duplicate_recall",
]
Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Positive = Annotated[int, Field(gt=0)]
DIMENSIONS: tuple[Dimension, ...] = (
    "audience_relevance", "summary_fidelity", "entity_extraction", "uncertainty",
)
COMPARABLE: tuple[Metric, ...] = (*DIMENSIONS, "duplicate_precision", "duplicate_recall")


class Source(StrictModel):
    source_id: Nonempty
    revision: Positive
    url: HttpUrl
    publisher: Nonempty
    source_class: Literal["primary", "secondary"]
    published_at: UtcDatetime | None
    retrieved_at: UtcDatetime
    locator: Nonempty
    excerpt: Annotated[str, Field(max_length=500)] | None
    fingerprint: Sha256


class SourcePin(StrictModel):
    source_id: Nonempty
    revision: Positive
    sha256: Sha256


class Observation(StrictModel):
    source: Source
    disposition: Disposition


class ExpectedObservation(Observation):
    event: Nonempty | None


class Claim(StrictModel):
    text: Nonempty
    source_ids: Annotated[tuple[Nonempty, ...], Field(min_length=1)]


class Event(StrictModel):
    event_id: Nonempty
    near_duplicate_group: Nonempty
    candidate_revision: Positive
    critical_duplicates: bool
    topics: Annotated[tuple[Topic, ...], Field(min_length=1)]
    entities: tuple[Nonempty, ...]
    warnings: tuple[Warning, ...]
    claims: Annotated[tuple[Claim, ...], Field(min_length=1)]


class EditorScore(StrictModel):
    output_sha256: Sha256
    adjudication: Adjudication
    scores: dict[Dimension, Score]

    @model_validator(mode="after")
    def validate_editor_scores(self):
        if self.adjudication.status != "human_adjudicated" or set(self.scores) != set(DIMENSIONS):
            raise ValueError("complete_editor_scores_required")
        return self


class Example(StrictModel):
    example_id: Nonempty
    category: Literal[
        "empty_day", "release_periods", "syndication", "correction", "inaccessible",
        "undated", "conflict", "adversarial", "stale",
    ]
    window_start: UtcDatetime
    window_end: UtcDatetime
    observations: tuple[ExpectedObservation, ...]
    events: tuple[Event, ...]
    rights: Rights
    label_origin: Literal["authored_fixture", "editor_adjudicated"]
    adjudication: Adjudication | None = None
    editor_score: EditorScore | None = None
    related_event_groups: tuple[Nonempty, ...] = ()
    forbidden_output: tuple[Nonempty, ...] = ()

    @model_validator(mode="after")
    def validate_ground_truth(self):
        event_ids = {event.event_id for event in self.events}
        source_ids = {item.source.source_id for item in self.observations}
        if any(not text.strip() for text in self.forbidden_output):
            raise ValueError("blank_forbidden_output")
        if self.window_start >= self.window_end:
            raise ValueError("invalid_window")
        if len(event_ids) != len(self.events) or len(source_ids) != len(self.observations):
            raise ValueError("duplicate_fixture_identity")
        members: dict[str, set[str]] = {event_id: set() for event_id in event_ids}
        for item in self.observations:
            published = item.source.published_at
            if item.disposition == "candidate":
                if item.event not in members or published is None or not self.window_start <= published < self.window_end:
                    raise ValueError("invalid_expected_candidate")
                members[item.event].add(item.source.source_id)
            elif item.event is not None:
                raise ValueError("withheld_item_in_event")
            elif item.disposition == "undated" and published is not None:
                raise ValueError("undated_item_has_date")
            elif item.disposition == "stale" and (published is None or published >= self.window_start):
                raise ValueError("invalid_stale_item")
            if published is not None and published > item.source.retrieved_at:
                raise ValueError("publication_after_retrieval")
        for event in self.events:
            if len({claim.text for claim in event.claims}) != len(event.claims):
                raise ValueError("duplicate_expected_claim")
            if not members[event.event_id] or any(
                len(set(claim.source_ids)) != len(claim.source_ids)
                or not set(claim.source_ids) <= members[event.event_id]
                for claim in event.claims
            ):
                raise ValueError("invalid_expected_claim_sources")
        if self.label_origin == "editor_adjudicated":
            if (
                self.adjudication is None or self.adjudication.status != "human_adjudicated"
                or self.rights.status != "reviewed_permitted"
            ):
                raise ValueError("adjudication_and_rights_required")
        elif self.adjudication is not None or self.editor_score is not None or self.rights.status != "synthetic_fixture":
            raise ValueError("fixture_must_not_claim_editor_review")
        return self


class Candidate(StrictModel):
    story_id: Nonempty
    revision: Positive
    representative: SourcePin
    members: Annotated[tuple[SourcePin, ...], Field(min_length=1)]
    claims: Annotated[tuple[Claim, ...], Field(min_length=1)]
    entities: tuple[Nonempty, ...]
    topics: Annotated[tuple[Topic, ...], Field(min_length=1)]
    warnings: tuple[Warning, ...]


class SavedScoutOutput(StrictModel):
    observations: tuple[Observation, ...]
    candidates: tuple[Candidate, ...]
    outcome: Literal["complete", "partial", "failed"]


class SavedExperiment(StrictModel):
    schema_version: Literal["1.0"]
    dataset_version: Nonempty
    dataset_manifest_sha256: Sha256
    evaluator_version: Literal["1.0.0"]
    split: Split
    prompt_version: Nonempty
    model_id: Nonempty
    # Validate the outer bundle first and score malformed outputs per example.
    outputs: dict[Nonempty, JsonValue]


class ReservedHoldout(StrictModel):
    status: Literal["reserved"]


class SealedHoldout(StrictModel):
    status: Literal["sealed"]
    sha256: Sha256


class Manifest(StrictModel):
    schema_version: Literal["1.0"]
    agent: Literal["news_scout"]
    dataset_version: Nonempty
    evaluator_version: Literal["1.0.0"]
    kind: Literal["authored_fixture", "editor_adjudicated"]
    dev_sha256: Sha256
    prod_regression_sha256: Sha256
    policy_sha256: Sha256
    holdout: Annotated[ReservedHoldout | SealedHoldout, Field(discriminator="status")]

    @model_validator(mode="after")
    def fixture_cannot_seal_holdout(self):
        if self.kind == "authored_fixture" and self.holdout.status == "sealed":
            raise ValueError("fixture_cannot_seal_holdout")
        return self


class Policy(StrictModel):
    schema_version: Literal["1.0"]
    policy_version: Nonempty
    rubric_floors: dict[Dimension, Score]
    regression_tolerances: dict[Metric, Score]
    calibration_min_examples: Annotated[int, Field(ge=3)]
    calibration_max_mean_error: Score

    @model_validator(mode="after")
    def validate_metrics(self):
        if set(self.rubric_floors) != set(DIMENSIONS) or set(self.regression_tolerances) != set(COMPARABLE):
            raise ValueError("invalid_policy_metrics")
        return self
