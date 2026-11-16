"""Reproducible local synthetic experiments, with private immutable bundles."""
import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
import re
import tempfile
from pathlib import Path
import subprocess
from uuid import uuid4
from opentelemetry.trace import StatusCode

from app.config import Settings
from app.contracts.common import Money, Usage
from app.contracts.execution import InputEnvelope, OutputEnvelope, SyntheticData
from app.evals.datasets import load_dataset
from app.evals.evaluators import Evaluator, REQUIRED, foundation_evaluators, policy_violation
from app.evals.models import (critical_observations, EvaluationPolicy, EvalResult, ExampleEvaluation, ExperimentBundle,
    ExperimentConfig, FixtureInput, LoadedDataset, MetricAggregate)
from app.execution.fakes import FakeStage
from app.execution.protocols import PolicyError
from app.execution.runner import AgentRunner
from app.observability.tracing import Telemetry
from app.prompts import load_prompt
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


class FixtureStage(FakeStage):
    """A policy-checked typed input wrapper; scenarios/references never drive output."""
    def __init__(self, source_refs: tuple[str, ...]):
        super().__init__()
        self.source_refs = source_refs

    def validate_input(self, value: FixtureInput) -> None:
        value = FixtureInput.model_validate_json(value.model_dump_json())
        if policy_violation(value, self.source_refs):
            raise PolicyError('fixture_policy_denied')

    def run(self, value: FixtureInput, context):
        return super().run(value.data, context)


def create_runner(root: Path) -> AgentRunner:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = open_database(root / 'state.db')
    migrate(conn)
    return AgentRunner(ArtifactStore(root / 'artifacts', conn),
        Telemetry(root / 'telemetry.db', exporter=object()),
        Settings(environment='test', fake_stage_enabled=True),
        load_prompt(Path('prompts/manifests/foundation.v1.json')))


def code_provenance() -> dict[str, str]:
    """Bind tracked changes and new source files without retaining their contents."""
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL).decode().strip()
    dirty = subprocess.check_output(['git', 'diff', 'HEAD', '--binary'], stderr=subprocess.DEVNULL)
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'], stderr=subprocess.DEVNULL)
    pins = []
    for raw in sorted(untracked.split(b'\0')):
        if not raw:
            continue
        path = Path(os.fsdecode(raw))
        if path.parts[0] in {'app', 'tests', 'evals', 'schemas', 'prompts', 'docs'} and path.is_file():
            pins.append((str(path), digest(path.read_bytes())))
    return {'code_commit':commit, 'dirty_diff_hash':digest(dirty + canonical(pins))}


def load_policy(path: str) -> EvaluationPolicy:
    policy = EvaluationPolicy.model_validate_json(Path(path).read_bytes())
    if tuple(policy.required_evaluators) != REQUIRED or set(policy.required_splits) != {'dev','holdout','prod_regression'}:
        raise ValueError('policy_coverage')
    return policy


def aggregate(records: tuple[ExampleEvaluation, ...], required=REQUIRED) -> dict[str, MetricAggregate]:
    aggregates = {}
    for name in required:
        results = [r for row in records for r in row.results if r.evaluator_id == name]
        denominator = sum(r.denominator or 0 for r in results)
        passes = sum(r.status == 'PASS' and r.value == 1.0 and r.denominator == 1 for r in results)
        aggregates[name] = MetricAggregate(samples=len(results), denominator=denominator,
            passes=passes, failures=sum(r.status == 'FAIL' for r in results),
            errors=sum(r.status == 'ERROR' for r in results), skipped=sum(r.status == 'SKIPPED' for r in results),
            rate=passes / denominator if denominator else None)
    return aggregates


def assess(records, expected_count, repeats, issues, expected_critical=None):
    issues = set(issues)
    if not expected_count or len(records) != expected_count * repeats:
        issues.add('result_incomplete')
    observations = critical_observations(records)
    if expected_critical is None:
        expected_critical = sum(row.severity == 'critical' and row.expected_outcome == 'reject' for row in records)
    critical = sum(observations) if (len(observations) == expected_critical
        and len(records) == expected_count * repeats) else None
    for row in records:
        if row.error or row.output is None or row.output.status == 'FAILED':
            issues.add('execution_error')
        ids = [r.evaluator_id for r in row.results]
        if sorted(ids) != sorted(REQUIRED):
            issues.add('result_incomplete')
        for result in row.results:
            if result.status == 'ERROR':
                issues.add('evaluator_error')
            elif (result.status == 'SKIPPED' or result.denominator != 1 or result.value is None
                  or result.metric != result.evaluator_id
                  or (result.status == 'PASS' and result.value != 1.0)
                  or (result.status == 'FAIL' and result.value != 0.0)):
                issues.add('result_incomplete')
    if critical is None:
        issues.add('result_incomplete')
    if 'execution_error' in issues or 'evaluator_error' in issues:
        status = 'ERROR'
    elif issues:
        status = 'INCOMPLETE'
    elif critical or any(r.status == 'FAIL' for row in records for r in row.results):
        status = 'FAIL'
    else:
        status = 'PASS'
    return status, critical, tuple(sorted(issues))


def attempt_evidence(attempts):
    complete = bool(attempts) and all(a.finished_at is not None for a in attempts)
    return dict(
        attempt_usages=tuple(OutputEnvelope[SyntheticData].model_validate_json(a.output_envelope).usage
                             if a.output_envelope else Usage() for a in attempts),
        retry_count=max((a.retry_count for a in attempts),default=0),
        latency_ms=sum(max(0.,(a.finished_at-a.started_at).total_seconds()*1000)
                       for a in attempts if a.finished_at is not None) if complete else None,
        started_at=min(a.started_at for a in attempts) if complete else None,
        finished_at=max(a.finished_at for a in attempts) if complete else None)


def run_experiment(config: ExperimentConfig, dataset: LoadedDataset, runner: AgentRunner,
                   evaluators: list[Evaluator]) -> ExperimentBundle:
    config = ExperimentConfig.model_validate_json(config.model_dump_json())
    policy = load_policy(config.policy_path)
    # Re-read the manifest and ALL split pins; LoadedDataset alone is not authority.
    authoritative = load_dataset(Path(config.dataset_root), dataset.split, 'gate')
    examples = tuple(dataset.iter_examples())
    expected = tuple(authoritative.iter_examples())
    issues = set()
    if ({k:v for k,v in dataset.summary().items() if k != 'purpose'} != {k:v for k,v in authoritative.summary().items() if k != 'purpose'} or not examples
            or tuple(row.canonical_bytes() for row in examples) != tuple(row.canonical_bytes() for row in expected)
            or dataset.kind != 'synthetic_foundation'):
        issues.add('dataset_incomplete')
    ids = [ev.evaluator_id for ev in evaluators]
    if sorted(ids) != sorted(policy.required_evaluators):
        issues.add('evaluator_coverage')
    schema_hash = digest(canonical({'input':InputEnvelope[FixtureInput].model_json_schema(),
        'fixture':FixtureInput.model_json_schema(), 'example':type(expected[0]).model_json_schema() if expected else {},
        'experiment':ExperimentBundle.model_json_schema()}))
    evaluator_pins = tuple((ev.evaluator_id,ev.version,ev.fingerprint(),digest(inspect.getsource(type(ev)).encode()))
                           for ev in evaluators)
    provenance = {**code_provenance(), 'dataset_hash':authoritative.dataset_hash,
        'dataset_version':authoritative.dataset_version, 'schema_hash':schema_hash,
        'prompt_hash':runner.prompt.sha256, 'prompt_version':runner.prompt.prompt_version,
        'config_hash':runner.settings.config_hash(), 'experiment_config_hash':config.record_hash(),
        'model_hash':runner.configuration.record_hash(), 'provider':runner.configuration.provider,
        'model':runner.configuration.model, 'parameters_hash':digest(canonical(runner.configuration.parameters)),
        'policy_hash':policy.record_hash(), 'policy_version':policy.policy_id,
        'evaluator_hash':digest(canonical(evaluator_pins))}
    versions = {ev.evaluator_id:ev.version for ev in evaluators}
    # Exact implementation and calibration bytes bind injected evaluators too.
    implementation_hash = digest(Path('app/evals/evaluators.py').read_bytes())
    provenance['evaluator_implementation_hash'] = implementation_hash
    rows = []
    for repeat in range(config.repeats):
        for example in examples:
            envelope = None
            try:
                value = FixtureInput.model_validate_json(example.input.model_dump_json())
                ref = runner.store.put(value.canonical_bytes(), 'application/json', '1.0')
                envelope = InputEnvelope[FixtureInput](input=value,input_ref=ref,
                    run_id=uuid4(),story_id=uuid4(),invocation_id=uuid4(),
                    agent_name='foundation',agent_version=FixtureStage.agent_version,
                    prompt_id=runner.prompt.prompt_id,prompt_version=runner.prompt.prompt_version,
                    prompt_hash=runner.prompt.sha256,config_version=runner.settings.config_version,
                    config_hash=runner.settings.config_hash(),created_at=datetime.now(timezone.utc))
                output = runner.run(FixtureStage(example.source_refs), envelope)
                attempts = runner.attempts(envelope.invocation_id)
                evidence = attempt_evidence(attempts)
            except Exception:
                try:
                    evidence = attempt_evidence(runner.attempts(envelope.invocation_id)) if envelope else {}
                except Exception:
                    evidence = {}
                rows.append(ExampleEvaluation(example_id=example.example_id,repeat=repeat,
                    severity=example.severity,expected_outcome=example.reference.outcome,
                    results=(),error='execution_error',**evidence))
                continue
            results = []
            for evaluator in evaluators:
                if evaluator.evaluator_id not in REQUIRED:
                    continue
                try:
                    with runner.telemetry.evaluator_span(output,evaluator.evaluator_id) as span:
                        emitted = [EvalResult.model_validate_json(result.model_dump_json())
                                   for result in evaluator.evaluate(example,output)]
                        if any(result.evaluator_id != evaluator.evaluator_id for result in emitted):
                            raise ValueError('evaluator_identity_mismatch')
                        if any(result.status == 'ERROR' for result in emitted):
                            span.set_status(StatusCode.ERROR)
                            span.add_event('evaluator.error')
                        results.extend(emitted)
                except Exception:
                    results.append(EvalResult(evaluator_id=evaluator.evaluator_id,metric=evaluator.evaluator_id,
                        status='ERROR',reason='evaluator_error'))
            rows.append(ExampleEvaluation(example_id=example.example_id,repeat=repeat,
                severity=example.severity,expected_outcome=example.reference.outcome,
                output=output,results=tuple(results),
                error='execution_error' if output.status == 'FAILED' else None,**evidence))
    records = tuple(rows)
    expected_critical = config.repeats * sum(
        e.severity == 'critical' and e.reference.outcome == 'reject' for e in expected)
    status, critical, issues = assess(records, authoritative.count, config.repeats, issues, expected_critical)
    return ExperimentBundle(config=config,model_configuration=runner.configuration,
        evaluator_versions=versions,evaluator_pins=evaluator_pins,expected_critical_outcomes=expected_critical,split=dataset.split,status=status,
        example_count=len(examples),expected_count=authoritative.count,provenance=provenance,
        records=records,aggregates=aggregate(records),critical_false_accepts=critical,
        usage=aggregate_usage(records),issues=issues)


def aggregate_usage(records: tuple[ExampleEvaluation, ...]) -> Usage:
    if not records:
        return Usage()
    usages = [usage for row in records for usage in (row.attempt_usages or (Usage(),))]
    def tokens(name):
        values = [getattr(u,name) for u in usages]
        return None if any(v is None for v in values) else sum(values)
    def money(name):
        values = [getattr(u,name) for u in usages]
        if any(v is None for v in values) or len({v.currency for v in values}) != 1:
            return None
        return Money(amount=sum(v.amount for v in values),currency=values[0].currency)
    input_tokens, output_tokens = tokens('input_tokens'),tokens('output_tokens')
    cost, estimated = money('measured_cost'),money('estimated_cost')
    return Usage(input_tokens=input_tokens,output_tokens=output_tokens,measured_cost=cost,
        estimated_cost=estimated,availability_reason='not_reported' if None in (input_tokens,output_tokens,cost) else None)


MANDATORY_PROVENANCE = frozenset({
    'code_commit','dirty_diff_hash','dataset_hash','dataset_version','schema_hash',
    'prompt_hash','prompt_version','config_hash','experiment_config_hash','model_hash',
    'provider','model','parameters_hash','policy_hash','policy_version','evaluator_hash',
    'evaluator_implementation_hash'})
HASH_PROVENANCE = frozenset(name for name in MANDATORY_PROVENANCE if name.endswith('_hash'))


def _validate_pins(bundle: ExperimentBundle) -> None:
    pins = bundle.provenance
    required = set(REQUIRED)
    versioned = set(bundle.evaluator_versions)
    if (not set(bundle.observed_evaluators) <= versioned
            or not versioned <= required
            or {name:version for name,version,_,_ in bundle.evaluator_pins} != bundle.evaluator_versions
            or pins.get('evaluator_hash') != digest(canonical(bundle.evaluator_pins))
            or (len(bundle.evaluator_pins) != len(versioned) and 'evaluator_coverage' not in bundle.issues)
            or (versioned != required and (bundle.status not in {'ERROR','INCOMPLETE'}
                or 'evaluator_coverage' not in bundle.issues))):
        raise ValueError('invalid_provenance')
    if (not MANDATORY_PROVENANCE.issubset(pins)
            or any(re.fullmatch('[0-9a-f]{64}',pins[key]) is None for key in HASH_PROVENANCE)
            or re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',pins['code_commit']) is None
            or pins['experiment_config_hash'] != bundle.config.record_hash()
            or pins['model_hash'] != bundle.model_configuration.record_hash()
            or pins['parameters_hash'] != digest(canonical(bundle.model_configuration.parameters))
            or pins['provider'] != bundle.model_configuration.provider
            or pins['model'] != bundle.model_configuration.model):
        raise ValueError('invalid_provenance')
    for row in bundle.records:
        output = row.output
        if output is None:
            continue
        if (len(row.attempt_usages) != row.retry_count + 1
                or row.attempt_usages[-1] != output.usage):
            raise ValueError('invalid_attempt_usage')
        if (output.agent_name != bundle.config.agent_name
                or output.prompt_hash != pins['prompt_hash']
                or output.prompt_version != pins['prompt_version']
                or output.config_hash != pins['config_hash']):
            raise ValueError('invalid_output_pin')
        if output.output is not None:
            data = output.output.canonical_bytes()
            ref = output.output_ref
            if ref.sha256 != digest(data) or ref.size_bytes != len(data) or ref.media_type != 'application/json':
                raise ValueError('invalid_output_pin')


def validate_bundle(bundle: ExperimentBundle) -> ExperimentBundle:
    bundle = ExperimentBundle.model_validate_json(bundle.model_dump_json())
    _validate_pins(bundle)
    status, critical, issues = assess(bundle.records,bundle.expected_count,bundle.config.repeats,bundle.issues,bundle.expected_critical_outcomes)
    memberships = {(r.example_id, r.repeat) for r in bundle.records}
    ids = {r.example_id for r in bundle.records}
    if (status != bundle.status or critical != bundle.critical_false_accepts
            or issues != bundle.issues or aggregate(bundle.records) != bundle.aggregates
            or aggregate_usage(bundle.records) != bundle.usage
            or len(memberships) != len(bundle.records)
            or (bundle.status == 'PASS' and (len(ids) != bundle.expected_count or bundle.example_count != bundle.expected_count))):
        raise ValueError('invalid_bundle')
    for row in bundle.records:
        if row.repeat >= bundle.config.repeats:
            raise ValueError('invalid_repeat')
    return bundle


def persist_bundle(bundle: ExperimentBundle, root: Path) -> Path:
    bundle = validate_bundle(bundle)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = canonical({'bundle_hash':bundle.record_hash(), 'bundle':bundle.model_dump(mode='json')})
    path = root / (bundle.record_hash() + '.json')
    fd, temporary_name = tempfile.mkstemp(dir=root,prefix='.pending-')
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard link atomically publishes complete bytes without overwriting.
            os.link(temporary,path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise ValueError('immutable_bundle_conflict') from None
        directory_fd = os.open(root,os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_bundle_bytes(raw: bytes) -> ExperimentBundle:
    """Parse an already captured private artifact without reopening its source."""
    try:
        doc=json.loads(raw)
        if set(doc)!={'bundle_hash','bundle'}:
            raise ValueError('invalid_bundle')
        bundle=validate_bundle(ExperimentBundle.model_validate_json(json.dumps(doc['bundle'])))
        if bundle.record_hash()!=doc['bundle_hash']:
            raise ValueError('bundle_integrity')
        return bundle
    except Exception:
        raise ValueError('invalid_bundle') from None


def read_bundle(path: Path) -> ExperimentBundle:
    try:
        bundle=read_bundle_bytes(path.read_bytes())
        if len(path.stem)==64 and path.stem!=bundle.record_hash():
            raise ValueError('bundle_integrity')
        return bundle
    except Exception:
        raise ValueError('invalid_bundle') from None


def exit_code(status: str) -> int:
    return 0 if status == 'PASS' else 1 if status == 'FAIL' else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Synthetic foundation harness only; private local bundles.')
    parser.add_argument('--agent', required=True, choices=['foundation'])
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--split', required=True, choices=['dev','holdout','prod_regression'])
    parser.add_argument('--output', type=Path, default=Path('data/evals'))
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--phoenix', action='store_true', help='Explicitly register existing local evidence in loopback Phoenix')
    args = parser.parse_args(argv)
    runner = None
    try:
        config = ExperimentConfig(dataset_root=args.dataset,repeats=args.repeats,seed=args.seed)
        dataset = load_dataset(Path(args.dataset),args.split,'gate')
        runner = create_runner(args.output / 'runtime')
        bundle = run_experiment(config,dataset,runner,foundation_evaluators())
        path = persist_bundle(bundle,args.output)
        summary={**bundle.summary(), 'bundle_path':str(path.resolve())}
        if args.phoenix:
            from app.evals.phoenix import PhoenixAdapter
            adapter=PhoenixAdapter(args.output/'phoenix-registry.db')
            offline=adapter.export_offline(bundle,args.output/'offline'/bundle.record_hash())
            registration=adapter.publish_experiment(bundle)
            summary.update(phoenix=registration.model_dump(mode='json'),offline_path=str(offline.resolve()))
        print(json.dumps(summary,sort_keys=True))
        if args.phoenix and registration.status != 'registered':
            return 2
        return exit_code(bundle.status)
    except Exception:
        print(json.dumps({'status':'ERROR','error':'evaluation_unavailable','synthetic':True}))
        return 2
    finally:
        if runner is not None:
            try:
                runner.telemetry.shutdown(0)
            finally:
                runner.store.conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
