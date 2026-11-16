"""Synthetic infrastructure checks; never editorial performance evidence."""
from datetime import datetime, timezone
from uuid import uuid4
import json
import pytest
from pydantic import ValidationError
from app.config import Settings
from app.contracts.execution import InputEnvelope
from app.execution.fakes import FakeStage, FakeProvider, SyntheticData
from app.execution.protocols import ModelRequest, ModelResponse, TransientProviderError, PolicyError
from app.execution.runner import AgentRunner, RequestConflict
from app.observability.tracing import Telemetry
from app.prompts import load_prompt
from app.storage.artifacts import ArtifactStore
from app.storage.db import open_database, migrate
from pathlib import Path

@pytest.fixture
def harness(tmp_path):
    conn = open_database(tmp_path/'state.db'); migrate(conn)
    store = ArtifactStore(tmp_path/'artifacts', conn)
    telemetry = Telemetry(tmp_path/'telemetry.db', exporter=object())
    settings = Settings(environment='test', fake_stage_enabled=True)
    prompt = load_prompt(Path('prompts/manifests/foundation.v1.json'))
    value = SyntheticData(value=4)
    ref = store.put(value.canonical_bytes(), 'application/json', '1.0')
    envelope = InputEnvelope[SyntheticData](run_id=uuid4(), story_id=uuid4(), invocation_id=uuid4(),
        agent_name='foundation', agent_version='1.0', prompt_id=prompt.prompt_id,
        prompt_version=prompt.prompt_version, prompt_hash=prompt.sha256,
        config_version=settings.config_version, config_hash=settings.config_hash(),
        created_at=datetime(2026,11,11,tzinfo=timezone.utc), input=value, input_ref=ref)
    delays = []
    runner = AgentRunner(store, telemetry, settings, prompt, sleeper=delays.append)
    yield runner, envelope, store, telemetry, delays
    conn.close()

# Removing typed persistence or pinned metadata must fail this check.
def test_success_has_typed_refs_versions_trace(harness):
    runner, envelope, store, telemetry, _ = harness
    output = runner.run(FakeStage(), envelope)
    assert output.status == 'OK' and output.output.value == 5
    assert store.read(output.output_ref) == output.output.canonical_bytes()
    assert output.run_id == envelope.run_id and output.invocation_id != envelope.invocation_id
    assert output.prompt_hash == envelope.prompt_hash and output.config_hash == envelope.config_hash
    records = telemetry.pending_records()
    assert {r.name for r in records} == {'pipeline.run', 'agent.invoke', 'model.call'}
    assert len({r.trace_id for r in records}) == 1
    attempt = next(r for r in records if r.name == 'agent.invoke')
    assert attempt.attributes['retry.count'] == 0
    assert attempt.attributes['model.provider'] == 'fake'
    assert attempt.attributes['prompt.hash'] == envelope.prompt_hash
    assert output.trace_id == attempt.trace_id
    assert runner.attempts(envelope.invocation_id)[0].output_envelope == output.model_dump_json()
    bundle = telemetry.export_offline(store.root.parent/'offline')
    assert json.loads((bundle/'manifest.json').read_text())['record_count'] == 3

# Retrying more than transient failures or changing logical identity must fail.
def test_timeout_three_attempts_stable_run_distinct_invocations(harness):
    runner, envelope, _, telemetry, delays = harness
    provider = FakeProvider(errors=[TransientProviderError('private')] * 3)
    output = runner.run(FakeStage(provider), envelope)
    attempts = runner.attempts(envelope.invocation_id)
    assert output.status == 'FAILED' and output.error.code == 'unavailable'
    assert delays == [1, 2] and len(attempts) == 3
    assert len({a.invocation_id for a in attempts}) == 3
    assert {a.run_id for a in attempts} == {envelope.run_id}
    assert [a.retry_count for a in attempts] == [0, 1, 2]
    assert {r.attributes['retry.count'] for r in telemetry.pending_records() if r.name == 'agent.invoke'} == {0,1,2}

@pytest.mark.parametrize('error,status', [(ValueError('bad schema'), 'FAILED'), (PolicyError('denied'), 'BLOCKED')])
def test_schema_policy_failure_no_retry(harness, error, status):
    runner, envelope, _, _, delays = harness
    output = runner.run(FakeStage(FakeProvider(errors=[error])), envelope)
    assert output.status == status and len(runner.attempts(envelope.invocation_id)) == 1
    assert delays == []

def test_unknown_usage_not_free(harness):
    output = harness[0].run(FakeStage(), harness[1])
    assert output.usage.measured_cost is None and output.usage.input_tokens is None
    assert output.usage.availability_reason == 'not_reported'

def test_fake_provider_cannot_publish(harness):
    runner, envelope, *_ = harness
    class TryingStage(FakeStage):
        def run(self, value, context):
            assert not hasattr(context, 'state_service')
            assert not hasattr(context, 'handoff')
            assert not hasattr(context, 'approve')
            assert not hasattr(context, 'store')
            return super().run(value, context)
    assert runner.run(TryingStage(), envelope).status == 'OK'
    assert not hasattr(FakeProvider(), 'publish')

@pytest.mark.parametrize('settings', [Settings(), Settings(environment='test')])
def test_fake_guard_applies_in_core(harness, settings):
    runner, envelope, *_ = harness
    runner.settings = settings
    with pytest.raises(PolicyError): runner.run(FakeStage(), envelope)
    assert runner.attempts(envelope.invocation_id) == []

def test_same_request_replays_without_stage_execution(harness):
    runner, envelope, *_ = harness
    output = runner.run(FakeStage(), envelope)
    class Never(FakeStage):
        def run(self, value, context): raise AssertionError('must not rerun')
    assert runner.run(Never(), envelope) == output
    assert len(runner.attempts(envelope.invocation_id)) == 1

def test_fingerprint_rejects_mutated_input(harness):
    runner, envelope, store, *_ = harness
    runner.run(FakeStage(), envelope)
    changed = SyntheticData(value=9)
    altered = envelope.model_copy(update={'input':changed, 'input_ref':store.put(changed.canonical_bytes(),'application/json','1.0')})
    with pytest.raises(RequestConflict): runner.run(FakeStage(), altered)

@pytest.mark.parametrize('field,value', [('prompt_hash','0'*64), ('config_hash','0'*64), ('agent_version','2.0')])
def test_pins_verified_before_execution(harness, field, value):
    runner, envelope, *_ = harness
    with pytest.raises(ValueError): runner.run(FakeStage(), envelope.model_copy(update={field:value}))
    assert runner.attempts(envelope.invocation_id) == []

def test_telemetry_down_execution_continues_degraded(harness):
    runner, envelope, _, telemetry, _ = harness
    telemetry.outbox.path = Path('/dev/null/unavailable.db')
    output = runner.run(FakeStage(), envelope)
    assert output.status == 'OK' and output.observability_status == 'DEGRADED'

def test_model_boundary_rejects_unapproved_provider(harness):
    runner, envelope, *_ = harness
    class RemoteStage(FakeStage):
        def run(self, value, context):
            context.generate(FakeProvider(), ModelRequest(provider='api', model='remote-v1', content=value))
            return value
    assert runner.run(RemoteStage(), envelope).status == 'BLOCKED'

# A completed journal cannot hide subsequently missing/corrupt artifacts.
def test_completed_replay_verifies_output_artifact(harness):
    from app.storage.artifacts import ArtifactIntegrityError
    runner, envelope, store, *_ = harness
    output = runner.run(FakeStage(), envelope)
    (store.root/output.output_ref.sha256[:2]/output.output_ref.sha256).write_bytes(b'corrupt')
    with pytest.raises(ArtifactIntegrityError): runner.run(FakeStage(), envelope)

# Summing only the last call would under-report a multi-call stage.
def test_multiple_model_calls_account_for_all_usage(harness):
    from app.contracts.common import Usage, Money
    from decimal import Decimal
    runner, envelope, *_ = harness
    responses = [ModelResponse(content=SyntheticData(value=5), usage=Usage(
        input_tokens=2,output_tokens=3,measured_cost=Money(amount=Decimal('0.01'),currency='USD'),
        availability_reason=None))] * 2
    class Twice(FakeStage):
        def run(self, value, context):
            context.generate(self.provider, ModelRequest(content=value))
            return context.generate(self.provider, ModelRequest(content=value)).content
    output = runner.run(Twice(FakeProvider(responses=responses)), envelope)
    assert output.usage.input_tokens == 4 and output.usage.output_tokens == 6
    assert output.usage.measured_cost.amount == Decimal('0.02')

def test_unknown_call_keeps_total_usage_unknown(harness):
    from app.contracts.common import Usage
    runner, envelope, *_ = harness
    responses = [ModelResponse(content=SyntheticData(value=5),usage=Usage(input_tokens=2,output_tokens=3)),
                 ModelResponse(content=SyntheticData(value=5))]
    class Twice(FakeStage):
        def run(self, value, context):
            context.generate(self.provider,ModelRequest(content=value))
            return context.generate(self.provider,ModelRequest(content=value)).content
    output = runner.run(Twice(FakeProvider(responses=responses)),envelope)
    assert output.usage.input_tokens is None and output.usage.measured_cost is None

@pytest.mark.parametrize('parameters', [{'temperature': float('nan')}, {'temperature': float('inf')}, {'secret_key':1}])
def test_model_parameters_require_safe_finite_values(parameters):
    with pytest.raises(ValueError): ModelRequest(content=SyntheticData(value=1),parameters=parameters)

# The provider wire format must retain payload fields, not just model metadata.
@pytest.mark.parametrize('boundary', [ModelRequest, ModelResponse])
def test_provider_boundary_roundtrip_preserves_typed_content(boundary):
    original = boundary(content=SyntheticData(value=7))
    wire = original.model_dump_json()
    assert json.loads(wire)['content'] == {'value':7}
    decoded = boundary.model_validate_json(wire)
    assert type(decoded.content) is SyntheticData
    assert decoded.content.value == 7

@pytest.mark.parametrize('boundary', [ModelRequest, ModelResponse])
def test_provider_boundary_specialized_schema_preserves_payload(boundary):
    typed_boundary = boundary[SyntheticData]
    original = typed_boundary(content=SyntheticData(value=7))
    decoded = typed_boundary.model_validate_json(original.model_dump_json())
    assert type(decoded.content) is SyntheticData and decoded.content.value == 7
    schema = typed_boundary.model_json_schema()
    assert schema['properties']['content']['$ref'] == '#/$defs/SyntheticData'
    assert schema['$defs']['SyntheticData']['required'] == ['value']
    assert schema['$defs']['SyntheticData']['properties']['value']['type'] == 'integer'
    assert schema['$defs']['SyntheticData']['additionalProperties'] is False

@pytest.mark.parametrize('boundary', [ModelRequest, ModelResponse])
@pytest.mark.parametrize('content', [{}, {'value':'7'}, {'value':7,'unexpected':True}])
def test_provider_boundary_typed_parsing_rejects_malformed_content(boundary, content):
    with pytest.raises(ValidationError):
        boundary[SyntheticData].model_validate_json(json.dumps({'content':content}))
