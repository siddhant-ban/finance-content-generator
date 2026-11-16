"""Recovery exercises real local storage and trusted StateService receipts."""
import pytest
from app.execution.fakes import FakeStage, SyntheticData
from app.execution.runner import AgentRunner, InterruptedExecution
from app.workflow.state import TransitionRequest
from app.contracts.workflow import StoryState
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from app.config import Settings
from app.contracts.execution import InputEnvelope
from app.observability.tracing import Telemetry
from app.prompts import load_prompt
from app.storage.artifacts import ArtifactStore
from app.storage.db import open_database, migrate

@pytest.fixture
def harness(tmp_path):
    conn = open_database(tmp_path/'state.db'); migrate(conn)
    store = ArtifactStore(tmp_path/'artifacts', conn)
    telemetry = Telemetry(tmp_path/'telemetry.db', exporter=object())
    settings = Settings(environment='test', fake_stage_enabled=True)
    prompt = load_prompt(Path('prompts/manifests/foundation.v1.json'))
    value = SyntheticData(value=4)
    envelope = InputEnvelope[SyntheticData](run_id=uuid4(), story_id=uuid4(), invocation_id=uuid4(),
        agent_name='foundation', agent_version='1.0', prompt_id=prompt.prompt_id,
        prompt_version=prompt.prompt_version, prompt_hash=prompt.sha256,
        config_version=settings.config_version, config_hash=settings.config_hash(),
        created_at=datetime(2026,11,11,tzinfo=timezone.utc), input=value,
        input_ref=store.put(value.canonical_bytes(),'application/json','1.0'))
    runner = AgentRunner(store,telemetry,settings,prompt,sleeper=lambda _:None)
    yield runner,envelope,store,telemetry,[]
    conn.close()

class Crash(BaseException): pass

def test_interrupted_attempt_explicit_on_restart(harness):
    runner, envelope, *_ = harness
    class Crashing(FakeStage):
        def run(self, value, context): raise Crash()
    with pytest.raises(Crash): runner.run(Crashing(), envelope)
    records = runner.recover_interrupted()
    assert len(records) == 1 and records[0].status == 'INTERRUPTED'
    with pytest.raises(InterruptedExecution): runner.run(FakeStage(), envelope)
    assert runner.recover_interrupted() == []

def test_crash_after_output_before_transition_no_double_advance(harness, tmp_path):
    runner, envelope, _, _, _ = harness
    # Same database/ArtifactStore must own the state journal and output.
    from app.workflow.policy import PolicyEngine
    from app.workflow.state import StateService
    from task4_fixtures import validator, artifact
    engine = PolicyEngine(runner.store)
    engine.register('candidate','fixture.v1','synthetic_foundation',validator)
    engine.register('cluster','fixture.v1','synthetic_foundation',validator)
    service = StateService(runner.store.conn, runner.store, engine)
    snapshot = service.create(envelope.story_id,envelope.run_id,artifact(runner.store,'candidate'),'test','create')
    class Cluster(FakeStage):
        def run(self, value, context):
            from app.contracts.common import StrictModel
            return ClusterData()
        def validate_output(self, value):
            ClusterData.model_validate(value.model_dump())
    from typing import Literal
    from app.contracts.common import StrictModel
    class ClusterData(StrictModel):
        schema_id: Literal['fixture.v1'] = 'fixture.v1'
        kind: Literal['synthetic_foundation'] = 'synthetic_foundation'
        label: Literal['fixture'] = 'fixture'
        stage: Literal['cluster'] = 'cluster'
    Cluster.output_type = ClusterData
    def handoff(output):
        return TransitionRequest(story_id=envelope.story_id,target=StoryState.CLUSTERED,
            expected_version=1,caller_scope='runner',idempotency_key=str(envelope.invocation_id),
            artifact=output.output_ref,trace_id=output.trace_id)
    runner.state_service = service; runner.handoff = handoff
    original = service.transition
    def crash_after_commit(request):
        original(request)
        raise Crash()
    service.transition = crash_after_commit
    with pytest.raises(Crash): runner.run(Cluster(), envelope)
    assert service.get(snapshot.story_id).version == 2
    service.transition = original
    recovered = runner.recover_interrupted()
    assert len(recovered) == 1 and recovered[0].status == 'COMPLETED'
    assert service.get(snapshot.story_id).version == 2
    assert runner.run(Cluster(), envelope).status == 'OK'
    assert len(runner.attempts(envelope.invocation_id)) == 1

def test_persist_failure_no_state_advance(harness):
    runner, envelope, store, *_ = harness
    def broken(*args, **kwargs): raise OSError('disk full')
    store.put = broken
    calls = []
    runner.handoff = lambda output: calls.append(output)
    with pytest.raises(OSError): runner.run(FakeStage(), envelope)
    assert calls == []
    assert runner.recover_interrupted()[0].status == 'INTERRUPTED'

def test_crash_in_handoff_builder_never_false_completes(harness):
    runner, envelope, *_ = harness
    runner.state_service = object()
    def crashing_builder(output): raise Crash()
    runner.handoff = crashing_builder
    with pytest.raises(Crash): runner.run(FakeStage(),envelope)
    assert runner.attempts(envelope.invocation_id)[0].status == 'STARTED'
    recovered = runner.recover_interrupted()
    assert recovered[0].status == 'INTERRUPTED'
    with pytest.raises(InterruptedExecution): runner.run(FakeStage(),envelope)
