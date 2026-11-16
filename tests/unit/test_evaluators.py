"""Harness gates must fail closed; these fixtures make no quality claim."""
from pathlib import Path
import pytest

from app.evals.datasets import load_dataset


@pytest.fixture
def execution(tmp_path):
    from app.evals.run import create_runner
    runner = create_runner(tmp_path)
    yield runner
    runner.telemetry.shutdown(0)
    runner.store.conn.close()


def experiment(execution, evaluators=None, dataset=None, **kwargs):
    from app.evals.run import run_experiment
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    dataset = dataset or load_dataset(Path('evals/foundation/1.0.0'), 'dev', 'gate')
    return run_experiment(ExperimentConfig(**kwargs), dataset, execution,
                          foundation_evaluators() if evaluators is None else evaluators)


def test_one_critical_failure_beats_high_mean(execution):
    from app.evals.evaluators import foundation_evaluators
    from app.evals.models import EvalResult
    evaluators = foundation_evaluators()
    class BadPolicy:
        evaluator_id = 'policy'
        version = '1.0'
        def fingerprint(self): return 'a' * 64
        def evaluate(self, example, output):
            return [EvalResult(evaluator_id='policy', metric='policy', status='FAIL' if example.example_id.endswith('2') else 'PASS', value=0.0 if example.example_id.endswith('2') else 1.0, denominator=1, severity=example.severity, reason='fixture_check')]
    evaluators[1] = BadPolicy()
    bundle = experiment(execution, evaluators)
    assert bundle.status == 'FAIL'
    assert len(bundle.private_failures()) == 1


def test_judge_timeout_error_not_zero_score(execution):
    from app.evals.evaluators import RubricEvaluator, foundation_evaluators
    def timeout(example, output): raise TimeoutError('private judge message')
    evaluators = foundation_evaluators()
    evaluators[-1] = RubricEvaluator(timeout)
    bundle = experiment(execution, evaluators)
    results = [r for row in bundle.records for r in row.results if r.evaluator_id == 'fake_rubric']
    assert bundle.status == 'ERROR'
    assert all(r.status == 'ERROR' and r.value is None for r in results)
    assert 'private judge message' not in bundle.model_dump_json()


def test_skipped_required_check_incomplete(execution):
    from app.evals.evaluators import foundation_evaluators
    from app.evals.models import EvalResult
    ev = foundation_evaluators()
    class Skipped(type(ev[0])):
        def evaluate(self, example, output):
            return [EvalResult(evaluator_id='contract', metric='contract', status='SKIPPED', reason='not_run')]
    ev[0] = Skipped()
    assert experiment(execution, ev).status == 'INCOMPLETE'


def test_missing_denominator_not_success(execution):
    from app.evals.evaluators import foundation_evaluators
    from app.evals.models import EvalResult
    ev = foundation_evaluators()
    class NoDenominator(type(ev[0])):
        def evaluate(self, example, output):
            return [EvalResult(evaluator_id='contract', metric='contract', status='PASS', value=1.0, reason='fixture_check')]
    ev[0] = NoDenominator()
    assert experiment(execution, ev).status == 'INCOMPLETE'


def test_bundle_provenance_and_reproducible_deterministic_scores(execution, tmp_path):
    from app.evals.run import persist_bundle, read_bundle
    a, b = experiment(execution, repeats=2, seed=42), experiment(execution, repeats=2, seed=42)
    assert a.status == b.status == 'PASS'
    assert a.aggregates == b.aggregates
    assert len(a.records) == 6 and a.example_count == 3
    assert a.provenance == b.provenance
    assert a.record_hash() != b.record_hash()  # actual trace/invocation identity is fresh
    for key in ('code_commit','dirty_diff_hash','dataset_hash','schema_hash','prompt_hash','config_hash','model_hash','policy_hash','evaluator_hash'):
        assert a.provenance[key]
    path = persist_bundle(a, tmp_path)
    assert path.stem == a.record_hash() and path.stat().st_mode & 0o777 == 0o600
    assert read_bundle(path) == a
    path.write_text('{}')
    with pytest.raises(ValueError): read_bundle(path)


def test_model_cost_missing_stays_unknown(execution):
    bundle = experiment(execution)
    assert bundle.usage.measured_cost is None and bundle.usage.input_tokens is None
    assert bundle.usage.availability_reason == 'not_reported'
    assert all(row.output.trace_id for row in bundle.records)


def test_mismatched_dataset_policy_not_comparable(execution):
    from app.evals.compare import compare
    a = experiment(execution)
    assert compare(a, a).status == 'PASS'
    for field in ('dataset_hash', 'policy_hash', 'evaluator_hash'):
        changed = a.model_copy(update={'provenance':{**a.provenance, field:'0'*64}})
        assert compare(changed, a).status == 'ERROR'


def test_empty_or_tampered_dataset_cannot_pass(execution):
    dataset = load_dataset(Path('evals/foundation/1.0.0'), 'dev', 'gate')
    dataset._examples = ()
    assert experiment(execution, dataset=dataset).status == 'INCOMPLETE'


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'unknown'])
def test_required_evaluator_coverage(execution, mutation):
    from app.evals.evaluators import foundation_evaluators
    ev = foundation_evaluators()
    if mutation == 'duplicate': ev.append(ev[0])
    elif mutation == 'missing': ev.pop()
    else: ev[0].evaluator_id = 'unknown'
    assert experiment(execution, ev).status == 'INCOMPLETE'


def test_missing_evaluator_round_trips_as_incomplete(execution, tmp_path):
    from app.evals.compare import compare
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import persist_bundle, read_bundle, validate_bundle

    evaluators = [ev for ev in foundation_evaluators() if ev.evaluator_id != 'fake_rubric']
    bundle = experiment(execution, evaluators)
    assert bundle.status == 'INCOMPLETE'
    assert bundle.issues == ('evaluator_coverage', 'result_incomplete')
    assert bundle.evaluator_versions == {ev.evaluator_id: ev.version for ev in evaluators}
    restored = read_bundle(persist_bundle(bundle, tmp_path))
    assert restored == bundle
    report = compare(restored, experiment(execution))
    assert (report.status, report.reason) == ('ERROR', 'not_comparable')
    versions = dict(restored.evaluator_versions)
    del versions['contract']
    with pytest.raises(ValueError, match='invalid_provenance'):
        validate_bundle(restored.model_copy(update={'evaluator_versions': versions}))


def test_fixture_wrapper_checks_input_not_expected_label(execution):
    from app.evals.run import FixtureStage
    from app.execution.protocols import PolicyError
    dataset = load_dataset(Path('evals/foundation/1.0.0'), 'dev', 'gate')
    good, bad, _ = tuple(dataset.iter_examples())
    stage = FixtureStage(good.source_refs)
    with pytest.raises(PolicyError): stage.validate_input(bad.input)
    stage.validate_input(good.input)
    # A scenario string by itself has no authority to bypass policy.
    with pytest.raises(PolicyError): stage.validate_input(bad.input.model_copy(update={'scenario':'increment'}))


def test_evaluator_timeout_and_duplicate_results_do_not_hide_other_examples(execution):
    from app.evals.evaluators import foundation_evaluators
    ev = foundation_evaluators()
    class Duplicated(type(ev[0])):
        def evaluate(self, example, output):
            result = super().evaluate(example, output)
            return result + result
    ev[0] = Duplicated()
    bundle = experiment(execution, ev)
    assert bundle.status == 'INCOMPLETE' and len(bundle.records) == 3


def test_changed_actual_examples_cannot_borrow_manifest_authority(execution):
    dataset = load_dataset(Path('evals/foundation/1.0.0'), 'dev', 'gate')
    examples = tuple(dataset.iter_examples())
    changed = examples[0].model_copy(update={'input':examples[0].input.model_copy(update={'data':examples[0].input.data.model_copy(update={'value':999})})})
    dataset._examples = (changed, *examples[1:])
    assert experiment(execution, dataset=dataset).status == 'INCOMPLETE'


def test_rubric_provenance_distinguishes_judge_implementations(execution):
    from app.evals.evaluators import foundation_evaluators, RubricEvaluator
    a = experiment(execution)
    def another_judge(example, output): return 1.0
    ev = foundation_evaluators(); ev[-1] = RubricEvaluator(another_judge)
    b = experiment(execution, ev)
    assert a.provenance['evaluator_hash'] != b.provenance['evaluator_hash']


def test_execution_unknown_error_is_safe_and_runs_remaining_examples(execution):
    original = execution.run
    def one_failure(stage, envelope):
        if envelope.input.data.value == 1: raise RuntimeError('secret source content')
        return original(stage, envelope)
    execution.run = one_failure
    bundle = experiment(execution)
    assert bundle.status == 'ERROR' and len(bundle.records) == 3
    assert len(bundle.private_failures()) == 1
    assert 'secret source content' not in bundle.model_dump_json()


def test_supplied_usage_is_aggregated_without_inventing_missing_measurements(execution,monkeypatch):
    from app.contracts.common import Usage, Money
    from app.execution.protocols import RunContext
    from decimal import Decimal
    measured=Usage(input_tokens=2,output_tokens=3,
        measured_cost=Money(amount=Decimal('0.01'),currency='USD'),availability_reason=None)
    monkeypatch.setattr(RunContext,'usage',property(lambda self:measured))
    bundle = experiment(execution)
    assert bundle.usage.input_tokens == 6 and bundle.usage.output_tokens == 9
    assert bundle.usage.measured_cost.amount == Decimal('0.03')


def test_comparison_keeps_private_failing_examples_and_redacts_summary(execution):
    from app.evals.compare import compare
    from app.evals.evaluators import foundation_evaluators
    from app.evals.models import EvalResult
    baseline = experiment(execution)
    ev = foundation_evaluators()
    # Same evaluator, different observed output: manufacture a candidate provider bug.
    original = execution.run
    def bad_output(stage, envelope):
        result = original(stage,envelope)
        if result.status == 'OK':
            changed = result.output.model_copy(update={'value':999})
            ref = execution.store.put(changed.canonical_bytes(),'application/json','1.0')
            return result.model_copy(update={'output':changed,'output_ref':ref})
        return result
    execution.run = bad_output
    candidate = experiment(execution, ev)
    report = compare(candidate, baseline)
    assert report.status == 'FAIL' and report.failing_examples == ('foundation-dev-1',)
    assert 'foundation-dev-1' not in str(report.summary())
    assert 'foundation-dev-1' not in repr(report)


def test_failed_runner_output_is_execution_error_not_quality_failure(execution):
    from app.contracts.execution import ExecutionError
    original = execution.run
    def unavailable(stage, envelope):
        output = original(stage,envelope)
        return output.model_copy(update={'status':'FAILED','output':None,'output_ref':None,
            'error':ExecutionError(code='unavailable',retryable=True)})
    execution.run = unavailable
    bundle = experiment(execution)
    assert bundle.status == 'ERROR' and len(bundle.records) == 3


def test_versioned_evaluation_schemas_export(tmp_path):
    import json
    from app.contracts.export import export_schemas
    names = export_schemas(tmp_path)
    assert 'evaluations.json' in names
    schema = json.loads((tmp_path/'evaluations.json').read_text())
    assert schema == json.loads(Path('schemas/v1/evaluations.json').read_text())
    assert 'ExperimentBundle' in schema['models'] and 'EvalResult' in schema['models']


def test_evaluation_traces_have_working_offline_export(execution, tmp_path):
    import json
    bundle = experiment(execution)
    exported = execution.telemetry.export_offline(tmp_path/'offline')
    manifest = json.loads((exported/'manifest.json').read_text())
    assert manifest['record_count'] >= 7
    assert all(row.output.trace_id for row in bundle.records)


def test_bundle_preserves_model_parameters_and_attempt_observations(execution):
    bundle = experiment(execution)
    assert bundle.model_configuration.provider == 'fake'
    assert bundle.model_configuration.parameters == {}
    assert bundle.evaluator_versions['policy'] == '1.0'
    assert all(row.retry_count == 0 and row.latency_ms is not None and row.latency_ms >= 0 for row in bundle.records)


def test_uncalibrated_constant_judge_cannot_pass(execution):
    from app.evals.evaluators import foundation_evaluators, RubricEvaluator
    def always_one(example, output): return 1.0
    evaluators = foundation_evaluators()
    evaluators[-1] = RubricEvaluator(always_one)
    bundle = experiment(execution, evaluators)
    assert bundle.status == 'ERROR'
    assert all(r.status == 'ERROR' and r.value is None for row in bundle.records for r in row.results if r.evaluator_id == 'fake_rubric')


def test_unpinned_judge_captured_state_is_rejected():
    from app.evals.evaluators import RubricEvaluator
    def factory(score):
        def judge(example, output): return score
        return judge
    for score in (1.0, 0.0):
        with pytest.raises(ValueError, match='unpinned_judge_state'):
            RubricEvaluator(factory(score))


def test_bundle_preserves_observed_attempt_times(execution):
    bundle = experiment(execution)
    for row in bundle.records:
        attempts = execution.store.conn.execute('SELECT payload FROM execution_attempts WHERE invocation_id=?',
            (str(row.output.invocation_id),)).fetchall()
        from app.contracts.execution import AttemptRecord
        attempt = AttemptRecord.model_validate_json(attempts[0][0])
        assert row.started_at == attempt.started_at
        assert row.finished_at == attempt.finished_at
        assert row.started_at <= row.finished_at


def test_example_observed_times_must_be_paired_and_ordered(execution):
    from datetime import datetime, timezone, timedelta
    from app.evals.models import ExampleEvaluation
    row = experiment(execution).records[0]
    start = datetime(2026,11,11,tzinfo=timezone.utc)
    for changes in ({'started_at':start,'finished_at':None},
                    {'started_at':None,'finished_at':start},
                    {'started_at':start,'finished_at':start-timedelta(seconds=1)}):
        payload = row.model_dump(mode='json')
        payload.update({key:value.isoformat() if value is not None else None for key,value in changes.items()})
        import json
        with pytest.raises(ValueError, match='observed_times'):
            ExampleEvaluation.model_validate_json(json.dumps(payload))


def test_malformed_judge_stays_error_without_numeric_score(execution):
    from app.evals.evaluators import foundation_evaluators, RubricEvaluator
    def malformed(example, output): return 'PASS'
    ev = foundation_evaluators(); ev[-1] = RubricEvaluator(malformed)
    bundle = experiment(execution, ev)
    assert bundle.status == 'ERROR'
    assert all(r.value is None and r.status == 'ERROR' for row in bundle.records for r in row.results if r.evaluator_id == 'fake_rubric')


def test_rubric_rejects_defaults_and_callable_instances():
    from app.evals.evaluators import RubricEvaluator
    def defaulted(example, output, score=1.0): return score
    class Judge:
        def __call__(self, example, output): return 1.0
    for judge in (defaulted,Judge()):
        with pytest.raises(ValueError,match='unpinned_judge_state'):
            RubricEvaluator(judge)


def test_judge_replacement_after_binding_is_error(execution):
    from app.evals.evaluators import foundation_evaluators
    ev = foundation_evaluators()
    def replacement(example, output): return 1.0
    ev[-1].judge = replacement
    bundle = experiment(execution,ev)
    assert bundle.status == 'ERROR'


_MUTABLE_JUDGE_STATE = {'score':1.0}

def _mutable_global_judge(example, output):
    return _MUTABLE_JUDGE_STATE['score']


def test_mutable_global_judge_configuration_is_rejected():
    from app.evals.evaluators import RubricEvaluator
    with pytest.raises(ValueError,match='unpinned_judge_state'):
        RubricEvaluator(_mutable_global_judge)


@pytest.mark.parametrize('relocation', ['outer', 'nested', 'both'])
def test_judge_binding_ignores_equivalent_code_filename_paths(relocation):
    from types import CodeType, FunctionType
    from app.evals.evaluators import _judge_binding, policy_violation
    code = policy_violation.__code__
    equivalent_path = str(Path(code.co_filename).parent) + '/./' + Path(code.co_filename).name
    constants = tuple(item.replace(co_filename=equivalent_path)
        if isinstance(item,CodeType) and relocation in ('nested','both') else item
        for item in code.co_consts)
    relocated = code.replace(co_consts=constants,
        co_filename=equivalent_path if relocation in ('outer','both') else code.co_filename)
    copied = FunctionType(relocated,policy_violation.__globals__,policy_violation.__name__)
    copied.__qualname__ = policy_violation.__qualname__
    copied.__module__ = policy_violation.__module__
    import inspect
    assert inspect.getsource(copied) == inspect.getsource(policy_violation)
    assert _judge_binding(copied) == _judge_binding(policy_violation)


def test_judge_binding_still_detects_semantic_code_constant_change():
    from types import FunctionType
    from app.evals.evaluators import _judge_binding, policy_violation
    code = policy_violation.__code__
    changed = code.replace(co_consts=tuple('9.9.9' if item == '1.0.0' else item for item in code.co_consts))
    copied = FunctionType(changed,policy_violation.__globals__,policy_violation.__name__)
    copied.__qualname__ = policy_violation.__qualname__
    copied.__module__ = policy_violation.__module__
    assert _judge_binding(copied) != _judge_binding(policy_violation)


def test_failed_envelope_cannot_borrow_passing_results(execution):
    from app.evals.run import validate_bundle
    from app.evals.compare import compare
    from app.contracts.execution import ExecutionError
    baseline = experiment(execution)
    row = baseline.records[0]
    output = row.output.model_copy(update={'status':'FAILED','output':None,'output_ref':None,
        'error':ExecutionError(code='unavailable',retryable=True)})
    candidate = baseline.model_copy(update={'records':(row.model_copy(update={'output':output,'error':None}),*baseline.records[1:])})
    with pytest.raises(ValueError): validate_bundle(candidate)
    assert compare(candidate,baseline).status == 'ERROR'


def test_passing_bundle_requires_all_producer_provenance_and_evaluator_versions(execution):
    from app.evals.run import validate_bundle
    from app.evals.compare import compare, COMPATIBLE_PINS
    baseline = experiment(execution)
    candidate = baseline.model_copy(update={'provenance':{key:baseline.provenance[key] for key in COMPATIBLE_PINS},'evaluator_versions':{}})
    with pytest.raises(ValueError): validate_bundle(candidate)
    assert compare(candidate,baseline).status == 'ERROR'


_NESTED_JUDGE_OPTIONS = {'score':1.0}

def _nested_mutable_global_judge(example, output):
    return 1.0 if any(_NESTED_JUDGE_OPTIONS['score'] for _ in (1,)) else 0.0


def test_nested_generator_mutable_judge_dependency_is_rejected():
    from app.evals.evaluators import RubricEvaluator
    with pytest.raises(ValueError,match='unpinned_judge_state'):
        RubricEvaluator(_nested_mutable_global_judge)


def test_interrupted_bundle_write_never_exposes_final_and_can_retry(execution,tmp_path,monkeypatch):
    from app.evals.run import persist_bundle,read_bundle
    import os
    bundle = experiment(execution)
    destination = tmp_path/'bundle-output'
    original_fdopen = os.fdopen
    class InterruptedFile:
        def __init__(self,stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self,*args): self.stream.close()
        def write(self,payload):
            self.stream.write(payload[:13]); self.stream.flush()
            raise OSError('simulated disk full')
    with monkeypatch.context() as patch:
        patch.setattr(os,'fdopen',lambda *args,**kwargs:InterruptedFile(original_fdopen(*args,**kwargs)))
        with pytest.raises(OSError): persist_bundle(bundle,destination)
    assert not (destination/(bundle.record_hash()+'.json')).exists()
    path = persist_bundle(bundle,destination)
    assert read_bundle(path) == bundle


@pytest.mark.parametrize('pin', ['code_commit','dirty_diff_hash','prompt_hash','config_hash','model_hash','parameters_hash','experiment_config_hash','provider','model','prompt_version'])
def test_invalid_mandatory_bundle_pin_is_rejected(execution,pin):
    from app.evals.run import validate_bundle
    bundle = experiment(execution)
    value = 'invalid' if pin in ('code_commit','dirty_diff_hash','prompt_hash','config_hash','model_hash','parameters_hash','experiment_config_hash') else 'different'
    candidate = bundle.model_copy(update={'provenance':{**bundle.provenance,pin:value}})
    with pytest.raises(ValueError): validate_bundle(candidate)


def test_output_artifact_pin_must_match_typed_payload(execution):
    from app.evals.run import validate_bundle
    bundle = experiment(execution)
    row = bundle.records[0]
    output = row.output.model_copy(update={'output':row.output.output.model_copy(update={'value':999})})
    candidate = bundle.model_copy(update={'records':(row.model_copy(update={'output':output}),*bundle.records[1:])})
    with pytest.raises(ValueError): validate_bundle(candidate)


def test_atomic_bundle_publication_allows_concurrent_identical_writers(execution,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.evals.run import persist_bundle,read_bundle
    bundle = experiment(execution)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _:persist_bundle(bundle,tmp_path/'concurrent'),range(8)))
    assert len(set(paths)) == 1 and read_bundle(paths[0]) == bundle
    assert not tuple((tmp_path/'concurrent').glob('.pending-*'))


def test_identical_writer_can_finish_while_first_payload_write_is_paused(execution,tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event,Lock
    import os
    from app.evals.run import persist_bundle,read_bundle
    bundle = experiment(execution)
    ready,release = Event(),Event()
    lock = Lock(); count = [0]
    fdopen = os.fdopen
    class PausedWrite:
        def __init__(self,stream): self.stream = stream
        def __getattr__(self,name): return getattr(self.stream,name)
        def __enter__(self): return self
        def __exit__(self,*args): self.stream.close()
        def write(self,payload):
            self.stream.write(payload[:13]); self.stream.flush(); ready.set()
            assert release.wait(5)
            self.stream.write(payload[13:])
            return len(payload)
    def wrapped(*args,**kwargs):
        stream = fdopen(*args,**kwargs)
        with lock:
            count[0] += 1
            return PausedWrite(stream) if count[0] == 1 else stream
    monkeypatch.setattr(os,'fdopen',wrapped)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(persist_bundle,bundle,tmp_path/'paused')
        try:
            assert ready.wait(5)
            second = pool.submit(persist_bundle,bundle,tmp_path/'paused')
            second_path = second.result(timeout=5)
        finally:
            release.set()
        first_path = first.result(timeout=5)
    assert first_path == second_path and read_bundle(first_path) == bundle


def test_valid_prompt_and_model_changes_remain_comparable(execution):
    from app.evals.run import validate_bundle
    from app.evals.compare import compare
    baseline = experiment(execution)
    configuration = baseline.model_configuration.model_copy(update={'model':'alternate-fake','parameters':{'temperature':0.0}})
    from app.evals.run import digest,canonical
    pins = {**baseline.provenance,'prompt_hash':'a'*64,'prompt_version':'foundation.v2',
        'model_hash':configuration.record_hash(),'model':configuration.model,
        'parameters_hash':digest(canonical(configuration.parameters))}
    records = tuple(row.model_copy(update={'output':row.output.model_copy(update={'prompt_hash':'a'*64,'prompt_version':'foundation.v2'})}) for row in baseline.records)
    candidate = baseline.model_copy(update={'model_configuration':configuration,'provenance':pins,'records':records})
    assert validate_bundle(candidate).status == 'PASS'
    assert compare(candidate,baseline).status == 'PASS'


def test_changed_evaluator_version_cannot_borrow_comparable_hash(execution):
    from app.evals.compare import compare
    baseline = experiment(execution)
    candidate = baseline.model_copy(update={'evaluator_versions':{**baseline.evaluator_versions,'policy':'2.0'}})
    assert compare(candidate,baseline).status == 'ERROR'


@pytest.mark.parametrize('pin', ['experiment_config_hash','model_hash','parameters_hash','config_hash','prompt_hash'])
def test_well_formed_hash_must_still_match_embedded_bundle_data(execution,pin):
    from app.evals.run import validate_bundle
    bundle = experiment(execution)
    candidate = bundle.model_copy(update={'provenance':{**bundle.provenance,pin:'0'*64}})
    with pytest.raises(ValueError): validate_bundle(candidate)


def test_atomic_publish_different_existing_bytes_remain_immutable_conflict(execution,tmp_path):
    from app.evals.run import persist_bundle
    bundle = experiment(execution)
    path = persist_bundle(bundle,tmp_path)
    path.write_bytes(b'different existing bytes')
    with pytest.raises(ValueError,match='immutable_bundle_conflict'):
        persist_bundle(bundle,tmp_path)
    assert path.read_bytes() == b'different existing bytes'
    assert not tuple(tmp_path.glob('.pending-*'))


@pytest.mark.parametrize('runner_failure', [False, True])
def test_compound_coverage_survives_persistence(execution,tmp_path,runner_failure,monkeypatch):
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import persist_bundle, read_bundle, validate_bundle
    evaluators = foundation_evaluators()[:-1]
    monkeypatch.setattr(evaluators[0], 'evaluate', lambda *args: [])
    if runner_failure:
        def fail(*args): raise RuntimeError()
        monkeypatch.setattr(execution, 'run', fail)
    b = read_bundle(persist_bundle(experiment(execution,evaluators),tmp_path))
    assert b.status == ('ERROR' if runner_failure else 'INCOMPLETE')
    assert 'contract' in b.evaluator_versions and 'contract' not in b.observed_evaluators
    versions = {**b.evaluator_versions, 'fake_rubric':'1.0'}
    with pytest.raises(ValueError):
        validate_bundle(b.model_copy(update={'evaluator_versions':versions}))


def test_evaluator_children_on_error_are_private(execution,tmp_path,monkeypatch):
    import json
    from app.evals.evaluators import foundation_evaluators
    evaluators = foundation_evaluators()
    def fail(*args): raise RuntimeError('private-error-text')
    monkeypatch.setattr(evaluators[-1],'evaluate',fail)
    dataset = load_dataset(Path('evals/foundation/1.0.0'),'holdout','gate')
    b = experiment(execution,evaluators,dataset)
    raw = (execution.telemetry.export_offline(tmp_path/'export')/'spans.jsonl').read_text()
    children = [v for v in map(json.loads,raw.splitlines()) if v['name']=='evaluator.run']
    assert len(children)==len(b.records)*len(evaluators)
    for row in b.records:
        group = [s for s in children if (s['trace_id'],s['parent_span_id'])==(row.output.trace_id,row.output.span_id)]
        assert {s['attributes']['evaluator.id'] for s in group}==set(b.evaluator_versions)
        error, = [s for s in group if s['status']=='ERROR']
        assert error['events'] and row.example_id not in raw
    assert 'private-error-text' not in raw


@pytest.mark.parametrize('unknown',[False,True])
def test_retry_usage_round_trip(execution,tmp_path,monkeypatch,unknown):
    from app.contracts.common import Usage, Money
    from app.execution.protocols import ModelResponse
    from app.execution.fakes import FakeStage,FakeProvider
    from app.evals.run import FixtureStage,persist_bundle,read_bundle,aggregate_usage
    from decimal import Decimal
    dataset = load_dataset(Path('evals/foundation/1.0.0'),'dev','gate')
    dataset._examples = tuple(dataset.iter_examples())[:1]
    calls=[]
    def response(self,request):
        if unknown and len(calls)==1: raise TimeoutError()
        return ModelResponse(content=request.content,usage=Usage(input_tokens=2,output_tokens=3,
            measured_cost=Money(amount=Decimal('.1'),currency='USD')))
    monkeypatch.setattr(FakeProvider,'generate',response)
    def retry(self,value,context):
        calls.append(1)
        result=FakeStage.run(self,value.data,context)
        if len(calls)==1: raise TimeoutError()
        return result
    monkeypatch.setattr(FixtureStage,'run',retry)
    execution.sleeper=lambda *_:None
    b=read_bundle(persist_bundle(experiment(execution,dataset=dataset),tmp_path))
    row,=b.records
    assert row.retry_count==1 and len(row.attempt_usages)==2
    assert [u.input_tokens for u in row.attempt_usages]==[None if unknown else 2,2]
    assert b.usage.input_tokens==(None if unknown else 4)
    assert b.usage.measured_cost==(None if unknown else Money(amount=Decimal('.2'),currency='USD'))


def test_missing_critical_outcomes_preserve_observations(execution,tmp_path,monkeypatch):
    from app.evals.run import persist_bundle,read_bundle
    original=execution.run
    calls=[]
    def mixed(stage,envelope):
        calls.append(1)
        if len(calls)==3: raise RuntimeError()
        if len(calls)==2: monkeypatch.setattr(stage,'validate_input',lambda *_:None)
        return original(stage,envelope)
    monkeypatch.setattr(execution,'run',mixed)
    b=read_bundle(persist_bundle(experiment(execution),tmp_path))
    s=b.summary()
    assert b.status=='ERROR' and s['critical_false_accepts'] is None
    assert (s['observed_critical_false_accepts'],s['observed_critical_outcomes'],s['expected_critical_outcomes'])==(1,1,2)


def test_interrupted_runner_preserves_retry_usage(execution,tmp_path,monkeypatch):
    from app.evals.run import persist_bundle,read_bundle
    original=execution.run
    def fail_after_journal(stage,envelope):
        original(stage,envelope)
        raise RuntimeError()
    monkeypatch.setattr(execution,'run',fail_after_journal)
    b=read_bundle(persist_bundle(experiment(execution),tmp_path))
    assert b.status=='ERROR' and all(row.output is None for row in b.records)
    assert all(len(row.attempt_usages)==1 for row in b.records)


def test_forged_config_hash_cannot_borrow_evaluator_fingerprint(execution):
    from app.evals.run import validate_bundle
    b=experiment(execution)
    versions={**b.evaluator_versions,'policy':'2.0'}
    with pytest.raises(ValueError):
        validate_bundle(b.model_copy(update={'evaluator_versions':versions,
            'evaluator_pins':tuple((i,versions[i],f,h) for i,_,f,h in b.evaluator_pins)}))
