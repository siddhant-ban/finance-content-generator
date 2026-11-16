"""Versioned dataset contracts; foundation expectations are fixture evidence only."""
from typing import Annotated, Iterator, Literal
from pathlib import Path

from pydantic import Field, PrivateAttr, model_validator

from app.contracts.common import Nonempty, Sha256, StrictModel, UtcDatetime
from app.contracts.execution import SyntheticData

Split = Literal['dev', 'holdout', 'prod_regression']
Purpose = Literal['tune', 'evaluate', 'gate']
AgentName = Literal['foundation']
Scenario = Literal['increment', 'invalid_reference', 'state_gate', 'changed_version', 'missing_metadata']


class FixtureInput(StrictModel):
    """Trap values are data for a later deterministic fixture stage, not valid evidence."""
    scenario: Scenario
    data: SyntheticData
    reference_ids: tuple[Nonempty, ...]
    state: Literal['DRAFT', 'AWAITING_APPROVAL', 'APPROVED_FOR_PUBLISH']
    artifact_version: Nonempty
    metadata_present: bool


class FixtureReference(StrictModel):
    outcome: Literal['accept', 'reject']
    output: SyntheticData | None
    rejection_code: Scenario | None

    @model_validator(mode='after')
    def outcome_content(self):
        if self.outcome == 'accept':
            if self.output is None or self.rejection_code is not None:
                raise ValueError('accept_requires_output')
        elif self.output is not None or self.rejection_code in (None, 'increment'):
            raise ValueError('reject_requires_code')
        return self


class FixtureLabels(StrictModel):
    target_behavior: Scenario
    critical_trap: bool


class Rights(StrictModel):
    status: Literal['synthetic_fixture', 'reviewed_permitted']
    provenance: Nonempty
    retention: Nonempty


class Adjudication(StrictModel):
    status: Literal['fixture_expected', 'human_adjudicated']
    labeler: Nonempty
    labeled_at: UtcDatetime
    rationale: Nonempty
    confidence: Annotated[float, Field(ge=0, le=1)]
    disagreement_history: tuple[Nonempty, ...]


class SplitFile(StrictModel):
    path: Nonempty
    sha256: Sha256
    example_ids: tuple[Nonempty, ...]
    event_groups: tuple[Nonempty, ...]
    evidence_fingerprints: tuple[Sha256, ...]


class DatasetManifest(StrictModel):
    schema_version: Literal['1.0']
    agent_name: AgentName
    agent_version: Nonempty
    dataset_version: Nonempty
    kind: Literal['synthetic_foundation', 'illustrative', 'adjudicated_agent']
    input_schema: Literal['foundation_fixture.input/1.0']
    reference_schema: Literal['foundation_fixture.reference/1.0']
    files: dict[Split, SplitFile]
    required_splits: tuple[Split, ...]
    label_taxonomy: tuple[Scenario, ...]
    rights: Rights
    adjudication: Adjudication
    limitations: tuple[Nonempty, ...]

    @model_validator(mode='after')
    def complete_splits_and_kind(self):
        if set(self.files) != {'dev', 'holdout', 'prod_regression'}:
            raise ValueError('required_split_files')
        if set(self.required_splits) != set(self.files) or len(self.required_splits) != 3:
            raise ValueError('required_split_membership')
        validate_provenance(self.kind, self.rights, self.adjudication)
        return self


class EvalExample(StrictModel):
    schema_version: Literal['1.0']
    example_id: Nonempty
    agent_name: AgentName
    agent_version: Nonempty
    dataset_version: Nonempty
    split: Split
    input: FixtureInput
    reference: FixtureReference
    labels: FixtureLabels
    severity: Literal['low', 'medium', 'high', 'critical']
    source_refs: tuple[Nonempty, ...]
    reference_ids: tuple[Nonempty, ...]
    event_group: Nonempty
    evidence_fingerprints: Annotated[tuple[Sha256, ...], Field(min_length=1)]
    rights: Rights
    adjudication: Adjudication

    @model_validator(mode='after')
    def consistent_fixture_expectation(self):
        rejecting = self.input.scenario != 'increment'
        if self.labels.target_behavior != self.input.scenario:
            raise ValueError('fixture_label_mismatch')
        if self.labels.critical_trap != rejecting:
            raise ValueError('fixture_trap_mismatch')
        if rejecting:
            if self.reference.outcome != 'reject' or self.reference.rejection_code != self.input.scenario or self.severity != 'critical':
                raise ValueError('fixture_rejection_required')
        elif self.reference.outcome != 'accept':
            raise ValueError('fixture_acceptance_required')
        return self


def validate_provenance(kind: str, rights: Rights, adjudication: Adjudication) -> None:
    if kind == 'synthetic_foundation':
        if rights.status != 'synthetic_fixture' or adjudication.status != 'fixture_expected':
            raise ValueError('synthetic_fixture_provenance_required')
    elif kind == 'adjudicated_agent':
        if rights.status != 'reviewed_permitted' or adjudication.status != 'human_adjudicated':
            raise ValueError('human_adjudication_and_rights_required')


class LoadedDataset(StrictModel):
    """Safe routine reports. Only iter_examples() grants selected evidence access.

    Private examples are excluded even from model_dump(include=...). This is an
    accidental-disclosure boundary, not protection against hostile local Python.
    """
    dataset_version: Nonempty
    dataset_hash: Sha256
    agent_name: AgentName
    kind: Literal['synthetic_foundation', 'adjudicated_agent']
    split: Split
    purpose: Purpose
    count: Annotated[int, Field(ge=0)]
    _examples: tuple[EvalExample, ...] = PrivateAttr(default=())
    _source_root: Path | None = PrivateAttr(default=None)

    def summary(self) -> dict[str, object]:
        return self.model_dump(mode='json')

    def iter_examples(self) -> Iterator[EvalExample]:
        """Privileged evaluator access; never use holdout for prompt iteration."""
        return iter(self._examples)

# Experiment schemas are versioned independently of the fixture schemas above.
from app.contracts.common import Usage
from app.contracts.execution import OutputEnvelope
from app.execution.protocols import ModelConfiguration


class EvalResult(StrictModel):
    evaluator_id: Nonempty
    metric: Nonempty
    status: Literal['PASS', 'FAIL', 'ERROR', 'SKIPPED']
    value: Annotated[float, Field(allow_inf_nan=False, ge=0, le=1)] | None = None
    denominator: Annotated[int, Field(gt=0)] | None = None
    severity: Literal['low', 'medium', 'high', 'critical'] = 'low'
    reason: Literal['fixture_check', 'judge_unavailable', 'judge_invalid', 'not_run', 'execution_error', 'evaluator_error']


class ExperimentConfig(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    agent_name: Literal['foundation'] = 'foundation'
    dataset_root: Nonempty = 'evals/foundation/1.0.0'
    policy_path: Nonempty = 'evals/policies/foundation.v1.json'
    repeats: Annotated[int, Field(ge=1, le=100)] = 1
    seed: int = 0


class EvaluationPolicy(StrictModel):
    schema_version: Literal['1.0']
    policy_id: Literal['foundation.v1']
    agent_name: Literal['foundation']
    synthetic_only: Literal[True]
    required_evaluators: tuple[Nonempty, ...]
    required_splits: tuple[Split, ...]
    contract_pass_rate: Literal[1.0]
    policy_pass_rate: Literal[1.0]
    critical_false_accepts: Literal[0]
    rubric_pass_rate: Literal[1.0]


class ExampleEvaluation(StrictModel):
    example_id: Nonempty
    repeat: Annotated[int, Field(ge=0)]
    severity: Literal['low', 'medium', 'high', 'critical']
    expected_outcome: Literal['accept', 'reject']
    output: OutputEnvelope[SyntheticData] | None = Field(default=None, repr=False)
    results: tuple[EvalResult, ...] = Field(repr=False)
    error: Literal['execution_error'] | None = None
    attempt_usages: tuple[Usage, ...] = ()
    retry_count: Annotated[int, Field(ge=0)] = 0
    latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None

    @model_validator(mode='after')
    def observed_times(self):
        if (self.started_at is None) != (self.finished_at is None):
            raise ValueError('observed_times_must_be_paired')
        if self.started_at is not None and self.finished_at < self.started_at:
            raise ValueError('observed_times_must_be_ordered')
        return self


def critical_observations(records):
    return tuple(row.output.status == 'OK' for row in records
                 if row.severity == 'critical' and row.expected_outcome == 'reject'
                 and row.output is not None and row.output.status in {'OK','BLOCKED'})


class MetricAggregate(StrictModel):
    samples: Annotated[int, Field(ge=0)]
    denominator: Annotated[int, Field(ge=0)]
    passes: Annotated[int, Field(ge=0)]
    failures: Annotated[int, Field(ge=0)]
    errors: Annotated[int, Field(ge=0)]
    skipped: Annotated[int, Field(ge=0)]
    rate: Annotated[float, Field(ge=0, le=1)] | None


class ExperimentBundle(StrictModel):
    schema_version: Literal['1.1'] = '1.1'
    synthetic: Literal[True] = True
    config: ExperimentConfig
    model_configuration: ModelConfiguration
    evaluator_versions: dict[Nonempty, Nonempty]
    evaluator_pins: tuple[tuple[Nonempty, Nonempty, Sha256, Sha256], ...]
    split: Split
    status: Literal['PASS', 'FAIL', 'ERROR', 'INCOMPLETE']
    example_count: Annotated[int, Field(ge=0)]
    expected_count: Annotated[int, Field(ge=0)]
    provenance: dict[Nonempty, Nonempty]
    records: tuple[ExampleEvaluation, ...] = Field(repr=False)
    aggregates: dict[Nonempty, MetricAggregate]
    expected_critical_outcomes: Annotated[int, Field(ge=0)]
    critical_false_accepts: Annotated[int, Field(ge=0)] | None
    usage: Usage
    issues: tuple[Literal['dataset_incomplete', 'evaluator_coverage', 'result_incomplete', 'execution_error', 'evaluator_error'], ...]

    @property
    def observed_evaluators(self):
        return sorted({r.evaluator_id for row in self.records for r in row.results})

    def summary(self) -> dict[str, object]:
        return {'schema_version': self.schema_version, 'synthetic': True, 'status':self.status,
                'split':self.split, 'example_count':self.example_count,
                'expected_count':self.expected_count, 'executions':len(self.records),
                'repeats':self.config.repeats, 'seed':self.config.seed,
                'configured_evaluators':sorted(self.evaluator_versions),
                'observed_evaluators':self.observed_evaluators,
                'observed_critical_outcomes':len(critical_observations(self.records)),
                'expected_critical_outcomes':self.expected_critical_outcomes,
                'observed_critical_false_accepts':sum(critical_observations(self.records)),
                'critical_false_accepts':self.critical_false_accepts, 'bundle_hash':self.record_hash()}

    def private_failures(self) -> tuple[ExampleEvaluation, ...]:
        return tuple(row for row in self.records if row.error or any(r.status != 'PASS' for r in row.results))


class ComparisonReport(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    status: Literal['PASS', 'FAIL', 'ERROR']
    reason: Literal['compatible', 'candidate_failed', 'not_comparable', 'invalid_bundle']
    candidate_hash: Sha256
    baseline_hash: Sha256
    failing_examples: tuple[Nonempty, ...] = Field(default=(), repr=False, exclude=True)

    def summary(self) -> dict[str, object]:
        return {**self.model_dump(mode='json'), 'failing_example_count':len(self.failing_examples)}
