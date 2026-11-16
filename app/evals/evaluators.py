"""Local deterministic checks and a calibrated synthetic rubric adapter.

The bundled judge validates harness wiring only. Real-agent use is prohibited.
"""
import hashlib
import dis
import inspect
import json
import marshal
import math
from pathlib import Path
from types import CodeType
from typing import Callable, Protocol

from app.contracts.execution import OutputEnvelope, SyntheticData
from app.evals.models import EvalExample, EvalResult, FixtureInput

REQUIRED = ('contract', 'policy', 'evidence', 'state', 'metadata', 'fake_rubric')


class Evaluator(Protocol):
    evaluator_id: str
    version: str
    def fingerprint(self) -> str: ...
    def evaluate(self, example: EvalExample, output: OutputEnvelope) -> list[EvalResult]: ...


def policy_violation(value: FixtureInput, source_refs: tuple[str, ...]) -> str | None:
    """Policy derives from input facts, never scenario or expected labels."""
    if not value.reference_ids or any(ref not in source_refs for ref in value.reference_ids):
        return 'invalid_reference'
    if value.state != 'AWAITING_APPROVAL':
        return 'state_gate'
    if value.artifact_version != '1.0.0':
        return 'changed_version'
    if not value.metadata_present:
        return 'missing_metadata'
    return None


def behavior_matches(example: EvalExample, output: OutputEnvelope) -> bool:
    if example.reference.outcome == 'accept':
        return output.status == 'OK' and output.output == example.reference.output
    return (output.status == 'BLOCKED' and output.error is not None
            and output.error.code == 'policy_denied'
            and policy_violation(example.input, example.source_refs) == example.reference.rejection_code)


class DeterministicEvaluator:
    version = '1.0'

    def __init__(self, evaluator_id: str = 'contract'):
        self.evaluator_id = evaluator_id

    def fingerprint(self) -> str:
        return hashlib.sha256((self.evaluator_id + ':' + self.version).encode()).hexdigest()

    def evaluate(self, example: EvalExample, output: OutputEnvelope) -> list[EvalResult]:
        if self.evaluator_id == 'contract':
            checked = OutputEnvelope[SyntheticData].model_validate_json(output.model_dump_json())
            passed = checked.agent_name == example.agent_name and checked.trace_id is not None
        elif self.evaluator_id == 'policy':
            passed = behavior_matches(example, output)
        elif self.evaluator_id in ('evidence', 'state', 'metadata'):
            relevant = {'evidence': 'invalid_reference', 'state': 'state_gate', 'metadata': 'missing_metadata'}[self.evaluator_id]
            violation = policy_violation(example.input, example.source_refs)
            passed = output.status == 'BLOCKED' if violation == relevant else behavior_matches(example, output)
        else:
            raise ValueError('unknown_evaluator')
        return [EvalResult(evaluator_id=self.evaluator_id, metric=self.evaluator_id,
            status='PASS' if passed else 'FAIL', value=1.0 if passed else 0.0,
            denominator=1, severity=example.severity, reason='fixture_check')]


def synthetic_judge(example: EvalExample, output: OutputEnvelope) -> float:
    return 1.0 if behavior_matches(example, output) else 0.0


def _portable_judge_code(code: CodeType) -> CodeType:
    """Remove checkout location, including nested comprehensions/generators."""
    return code.replace(co_filename='<judge>',co_consts=tuple(
        _portable_judge_code(item) if isinstance(item,CodeType) else item
        for item in code.co_consts))


def _judge_binding(judge: Callable) -> str:
    """Only stateless inspectable Python functions and immutable globals are supported.

    Reject closures/defaults/instances rather than imply their hidden state is pinned.
    Recheck this binding before and after calibration and each real evaluation.
    """
    seen = set()
    def bind(value):
        if value is None or type(value) in (bool, int, str):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) is tuple:
            return ['tuple', [bind(item) for item in value]]
        if value is any:
            return ['builtin','any']
        if isinstance(value,type) and value.__module__ == 'builtins' and issubclass(value,Exception):
            return ['exception',value.__name__]
        if not inspect.isfunction(value) or value.__closure__ or value.__defaults__ or value.__kwdefaults__ or value.__dict__:
            raise ValueError('unpinned_judge_state')
        if id(value) in seen:
            raise ValueError('unpinned_judge_state')
        seen.add(id(value))
        try:
            def global_names(code):
                names = {instruction.argval for instruction in dis.get_instructions(code)
                    if instruction.opname in {'LOAD_GLOBAL','LOAD_NAME'}}
                for constant in code.co_consts:
                    if isinstance(constant,CodeType):
                        names.update(global_names(constant))
                return names
            dependencies = {}
            for name in sorted(global_names(value.__code__)):
                if name in value.__globals__:
                    dependencies[name] = bind(value.__globals__[name])
                elif name in value.__builtins__:
                    dependencies[name] = bind(value.__builtins__[name])
                else:
                    raise ValueError('unpinned_judge_state')
            return {'function':value.__module__ + '.' + value.__qualname__,
                'source':hashlib.sha256(inspect.getsource(value).encode()).hexdigest(),
                'code':hashlib.sha256(marshal.dumps(_portable_judge_code(value.__code__), 2)).hexdigest(),
                'dependencies':dependencies}
        finally:
            seen.remove(id(value))
    try:
        return hashlib.sha256(json.dumps(bind(judge),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    except Exception:
        raise ValueError('unpinned_judge_state') from None


class RubricEvaluator:
    evaluator_id = 'fake_rubric'
    version = '1.0'

    def __init__(self, judge: Callable = synthetic_judge, *, calibration_path: Path = Path('tests/fixtures/evals/judge_calibration.json')):
        self.judge = judge
        self.calibration_bytes = calibration_path.read_bytes()
        calibration = json.loads(self.calibration_bytes)
        if calibration.get('synthetic_only') is not True:
            raise ValueError('invalid_calibration')
        self.calibration_example = EvalExample.model_validate_json(json.dumps(calibration['example']))
        if self.calibration_example.agent_name != 'foundation' or self.calibration_example.rights.status != 'synthetic_fixture':
            raise ValueError('invalid_calibration')
        self.anchors = tuple((OutputEnvelope[SyntheticData].model_validate_json(json.dumps(anchor['output'])),anchor['score'])
            for anchor in calibration['anchors'])
        if (len(self.anchors) != 2 or tuple(score for _,score in self.anchors) != (1.0,0.0)
                or tuple(behavior_matches(self.calibration_example,output) for output,_ in self.anchors) != (True,False)):
            raise ValueError('invalid_calibration')
        self.judge_ref = calibration['judge']
        self.judge_version = calibration['judge_version']
        self.prompt_ref = calibration['prompt']
        self.prompt_version = calibration['prompt_version']
        self.calibration_ref = calibration['calibration_id']
        self.calibration_hash = hashlib.sha256(self.calibration_bytes).hexdigest()
        # Source identity is provenance, never a claim of judge correctness.
        self.judge_hash = _judge_binding(judge)

    def fingerprint(self) -> str:
        return hashlib.sha256((self.evaluator_id + self.version + self.calibration_hash + self.judge_hash
            + self.judge_ref + self.judge_version + self.prompt_ref + self.prompt_version).encode()).hexdigest()

    def evaluate(self, example: EvalExample, output: OutputEnvelope) -> list[EvalResult]:
        try:
            if example.agent_name != 'foundation' or example.rights.status != 'synthetic_fixture':
                raise ValueError('synthetic_only')
            if _judge_binding(self.judge) != self.judge_hash:
                raise ValueError('judge_binding_changed')
            # Exercise both typed anchors against the exact callable, not its labels.
            for anchor, expected in self.anchors:
                observed = self.judge(
                    EvalExample.model_validate_json(self.calibration_example.model_dump_json()),
                    OutputEnvelope[SyntheticData].model_validate_json(anchor.model_dump_json()))
                if type(observed) not in (float,int) or observed != expected:
                    raise ValueError('uncalibrated_judge')
            if _judge_binding(self.judge) != self.judge_hash:
                raise ValueError('judge_binding_changed')
            score = self.judge(example, output)
            if _judge_binding(self.judge) != self.judge_hash:
                raise ValueError('judge_binding_changed')
            if type(score) not in (float, int) or score not in (0.0, 1.0):
                raise ValueError('invalid_judge_score')
        except (TimeoutError, ConnectionError):
            return [EvalResult(evaluator_id=self.evaluator_id,metric=self.evaluator_id,status='ERROR',reason='judge_unavailable')]
        except Exception:
            return [EvalResult(evaluator_id=self.evaluator_id,metric=self.evaluator_id,status='ERROR',reason='judge_invalid')]
        return [EvalResult(evaluator_id=self.evaluator_id,metric=self.evaluator_id,
            status='PASS' if score == 1.0 else 'FAIL', value=float(score),denominator=1,
            severity=example.severity,reason='fixture_check')]


def foundation_evaluators() -> list[Evaluator]:
    return [DeterministicEvaluator(name) for name in REQUIRED[:-1]] + [RubricEvaluator()]
