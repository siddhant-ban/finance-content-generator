"""Durable fake-stage runner with bounded retries and explicit interruption.

External work is never assumed exactly-once. Only stored, validated outputs can
be handed to a constructor-bound StateService; recovery replays the saved request.
"""
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import UUID, uuid4
from app.config import Settings
from app.contracts.common import StrictModel
from app.contracts.execution import AttemptRecord, InputEnvelope, OutputEnvelope, ExecutionError, InvocationMetadata
from app.execution.protocols import ModelConfiguration, PolicyError, RunContext, Stage, TransientProviderError, usage_attributes
from app.prompts import PromptSpec, load_prompt
from app.workflow.state import TransitionRequest

class RequestConflict(ValueError):
    """A logical invocation was reused with different immutable input."""

class InterruptedExecution(RuntimeError):
    """Uncertain work requires a deliberate new logical request."""

class AgentRunner:
    def __init__(self, store, telemetry, settings: Settings, prompt: PromptSpec, *,
                 configuration: ModelConfiguration | None = None, state_service=None,
                 handoff: Callable[[OutputEnvelope], TransitionRequest] | None = None,
                 clock=lambda: datetime.now(timezone.utc), sleeper=time.sleep):
        self.store = store
        self.telemetry = telemetry
        self.settings = settings
        self.prompt = prompt
        self.configuration = configuration or ModelConfiguration()
        self.state_service = state_service
        self.handoff = handoff
        self.clock = clock
        self.sleeper = sleeper

    def attempts(self, request_id: UUID) -> list[AttemptRecord]:
        return [AttemptRecord.model_validate_json(row[0]) for row in self.store.conn.execute(
            'SELECT payload FROM execution_attempts WHERE request_id=? ORDER BY retry_count', (str(request_id),))]

    def _save(self, record: AttemptRecord, *, insert=False):
        conn = self.store.conn
        if conn.in_transaction:
            raise ValueError('attempt persistence requires idle connection')
        with conn:
            if insert:
                conn.execute('INSERT INTO execution_attempts VALUES (?,?,?,?,?)',
                    (str(record.invocation_id),str(record.request_id),record.retry_count,record.status,record.model_dump_json()))
            else:
                conn.execute('UPDATE execution_attempts SET status=?,payload=? WHERE invocation_id=?',
                    (record.status,record.model_dump_json(),str(record.invocation_id)))

    def _handoff(self, record: AttemptRecord) -> AttemptRecord:
        if record.handoff_request is not None:
            if self.state_service is None:
                raise PolicyError('trusted state service required for saved handoff')
            request = TransitionRequest.model_validate_json(record.handoff_request)
            self.state_service.transition(request)
        completed = record.model_copy(update={'status':'COMPLETED', 'finished_at':self.clock()})
        self._save(completed)
        return completed

    def recover_interrupted(self) -> list[AttemptRecord]:
        """Call only after stopping other runners; this is not a worker lease."""
        recovered = []
        rows = self.store.conn.execute(
            "SELECT payload FROM execution_attempts WHERE status IN ('STARTED','OUTPUT_STORED') ORDER BY rowid").fetchall()
        for row in rows:
            record = AttemptRecord.model_validate_json(row[0])
            if record.status == 'OUTPUT_STORED':
                output = json.loads(record.output_envelope)
                from app.contracts.common import ArtifactRef
                self.store.read(ArtifactRef.model_validate_json(json.dumps(output['output_ref'])))
                recovered.append(self._handoff(record))
            else:
                record = record.model_copy(update={'status':'INTERRUPTED', 'finished_at':self.clock()})
                self._save(record)
                recovered.append(record)
        return recovered

    def _pins(self, stage, envelope):
        settings = Settings.model_validate(self.settings.model_dump())
        if not settings.fake_stage_enabled or settings.environment not in {'test','demo'}:
            raise PolicyError('fake stages require explicit test/demo configuration')
        prompt = PromptSpec.model_validate(self.prompt.model_dump())
        # The only runnable prompt in this foundation is checked against disk.
        if load_prompt(Path('prompts/manifests/foundation.v1.json')) != prompt:
            raise ValueError('unverified foundation prompt')
        config = ModelConfiguration.model_validate_json(self.configuration.model_dump_json())
        if config.provider != 'fake':
            raise PolicyError('only fake execution is available')
        for field in ('agent_name','agent_version','prompt_id','prompt_version'):
            if getattr(envelope,field) != getattr(stage,field):
                raise ValueError('stage metadata mismatch')
        if (envelope.prompt_id != prompt.prompt_id or envelope.prompt_version != prompt.prompt_version
                or envelope.prompt_hash != prompt.sha256 or envelope.config_version != settings.config_version
                or envelope.config_hash != settings.config_hash()):
            raise ValueError('pinned configuration mismatch')
        return settings, config

    def run(self, stage: Stage, envelope: InputEnvelope) -> OutputEnvelope:
        # Round-trip avoids trusting model_construct/model_copy or nested mutation.
        envelope = type(envelope).model_validate_json(envelope.model_dump_json())
        settings, configuration = self._pins(stage,envelope)
        if self.store.read(envelope.input_ref) != envelope.input.canonical_bytes():
            raise ValueError('input does not match registered immutable reference')
        input_json = envelope.model_dump_json()
        fingerprint = hashlib.sha256(json.dumps({
            'input':json.loads(input_json), 'configuration':configuration.model_dump(mode='json')},
            sort_keys=True,separators=(',',':')).encode()).hexdigest()
        conn = self.store.conn
        if conn.in_transaction: raise ValueError('runner requires idle connection')
        with conn:
            existing = conn.execute('SELECT fingerprint FROM execution_requests WHERE request_id=?',
                                    (str(envelope.invocation_id),)).fetchone()
            if existing is not None and existing[0] != fingerprint:
                raise RequestConflict('logical request input changed')
            if existing is None:
                conn.execute('INSERT INTO execution_requests VALUES (?,?,?,?)',
                    (str(envelope.invocation_id), fingerprint,input_json,configuration.model_dump_json()))
        previous = self.attempts(envelope.invocation_id)
        if previous:
            record = previous[-1]
            if record.status in {'STARTED','INTERRUPTED'}:
                raise InterruptedExecution('explicit recovery/new logical request required')
            if record.status == 'OUTPUT_STORED':
                record = self._handoff(record)
            if record.output_envelope:
                result = OutputEnvelope[stage.output_type].model_validate_json(record.output_envelope)
                if result.output is not None:
                    if self.store.read(result.output_ref) != result.output.canonical_bytes():
                        raise ValueError('saved output differs from immutable artifact')
                    stage.validate_output(result.output)
                return result
            raise InterruptedExecution('unfinished retry sequence requires new logical request')
        metadata = {name:getattr(envelope,name) for name in InvocationMetadata.model_fields}
        final_record = None
        final_output = None
        # Telemetry persists locally; no flush or remote request happens here.
        with self.telemetry.story_context(envelope.run_id,envelope.story_id,envelope.parent_context):
            for retry in range(3):
                invocation = uuid4()
                pinned = dict(metadata, invocation_id=invocation)
                attrs = {
                    'agent.name':envelope.agent_name,'agent.version':envelope.agent_version,
                    'prompt.version':envelope.prompt_version,'prompt.hash':envelope.prompt_hash,
                    'config.version':envelope.config_version,'config.hash':envelope.config_hash,
                    'invocation.id':str(invocation),'input.ref':envelope.input_ref.sha256,
                    'retry.count':retry,'model.provider':configuration.provider,
                    'model.name':configuration.model,'model.parameters':configuration.parameters,
                    'environment':settings.environment}
                with self.telemetry.span('agent.invoke','finance-media-orchestrator',attrs) as span:
                    context = span.get_span_context()
                    record = AttemptRecord(**pinned,request_id=envelope.invocation_id,
                        input_fingerprint=fingerprint,input_ref=envelope.input_ref,retry_count=retry,
                        status='STARTED',provider=configuration.provider,model=configuration.model,
                        parameters=configuration.parameters,trace_id=f'{context.trace_id:032x}',
                        span_id=f'{context.span_id:016x}',started_at=self.clock())
                    self._save(record,insert=True)
                    runtime = RunContext(self.telemetry, configuration)
                    failure = None
                    try:
                        # A fresh copy protects the pinned request against in-place mutation.
                        value = type(envelope.input).model_validate_json(envelope.input.model_dump_json())
                        stage.validate_input(value)
                        output = stage.run(value,runtime)
                        output = stage.output_type.model_validate_json(output.model_dump_json())
                        stage.validate_output(output)
                    except PolicyError:
                        failure = ExecutionError(code='policy_denied',retryable=False)
                    except (TransientProviderError, TimeoutError, ConnectionError):
                        failure = ExecutionError(code='unavailable',retryable=True)
                    except (ValueError, TypeError):
                        failure = ExecutionError(code='invalid_input',retryable=False)
                    except Exception:
                        failure = ExecutionError(code='exception',retryable=False)
                    if failure is not None:
                        status = 'BLOCKED' if failure.code == 'policy_denied' else 'FAILED'
                        result = OutputEnvelope[stage.output_type](**pinned,status=status,error=failure,
                            usage=runtime.usage,trace_id=record.trace_id,span_id=record.span_id)
                        record = record.model_copy(update={'status':status,'finished_at':self.clock(),
                                                         'output_envelope':result.model_dump_json()})
                        self._save(record)
                        span.set_attributes({'result.status':status,'error.code':failure.code})
                    else:
                        ref = self.store.put(output.canonical_bytes(),'application/json','1.0')
                        result = OutputEnvelope[stage.output_type](**pinned,status='OK',output=output,
                            output_ref=ref,usage=runtime.usage,trace_id=record.trace_id,span_id=record.span_id)
                        # Record the output before invoking any trusted handoff builder.
                        record = record.model_copy(update={'status':'OUTPUT_STORED',
                                                          'output_envelope':result.model_dump_json()})
                        if self.handoff is not None:
                            if self.state_service is None: raise PolicyError('trusted state service required')
                            request = self.handoff(result)
                            request = TransitionRequest.model_validate(request.model_dump())
                            if request.story_id != envelope.story_id or request.artifact != ref:
                                raise PolicyError('handoff must bind the stored output and story')
                            record = record.model_copy(update={'handoff_request':request.model_dump_json()})
                        self._save(record)
                        record = self._handoff(record)
                        span.set_attributes({'result.status':'OK','output.ref':ref.sha256})
                    span.set_attributes(usage_attributes(runtime.usage))
                    final_output, final_record = result, record
                if failure is not None and failure.retryable and retry < 2:
                    self.sleeper(min(1 * 2**retry,30))
                    continue
                break
        # on_end has now run for both root and attempt; assess actual persistence.
        health = self.telemetry.health()
        final_output = final_output.model_copy(update={
            'observability_status':'DEGRADED' if health.degraded else 'HEALTHY'})
        final_record = final_record.model_copy(update={'output_envelope':final_output.model_dump_json()})
        self._save(final_record)
        return final_output
