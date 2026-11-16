"""Optimistic, idempotent editorial transitions with atomic immutable audit."""
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime,timezone
from typing import Annotated, Literal
from uuid import UUID,uuid4
from pydantic import Field
from app.contracts.common import StrictModel,ArtifactRef,Nonempty,Sha256,UtcDatetime
from app.contracts.workflow import StoryState,ReviewManifest,ChangeKind
from app.contracts.evidence import validate_evidence,validate_claim_uses
from app.storage.artifacts import ArtifactStore
from app.workflow.policy import PolicyEngine,MEMBERS

NORMAL=tuple(StoryState)[:9]
STAGES=('candidate','cluster','score','dossier','verification','script','render')
FORWARD=dict(zip(NORMAL[:7],NORMAL[1:8]))
STAGE_FOR=dict(zip(NORMAL[:7],STAGES))

def allowed_edge(source: StoryState,target: StoryState) -> bool:
    return bool(FORWARD.get(source)==target or
        (source in NORMAL and target in {StoryState.REJECTED,StoryState.NEEDS_RESEARCH,StoryState.NEEDS_REVISION}) or
        (source in {StoryState.NEEDS_RESEARCH,StoryState.NEEDS_REVISION} and target==StoryState.REJECTED) or
        (source==StoryState.NEEDS_RESEARCH and target in NORMAL[:3]) or
        (source==StoryState.NEEDS_REVISION and target in NORMAL[:7]) or
        (source in NORMAL[7:9] and target==StoryState.AWAITING_APPROVAL) or
        (source==StoryState.AWAITING_APPROVAL and target==StoryState.APPROVED_FOR_PUBLISH))

class StateConflict(ValueError):
    """An optimistic version or idempotency fingerprint conflicts."""
class PolicyDenied(ValueError):
    """A state edge or its current immutable evidence is not eligible."""

class TransitionRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    story_id: UUID
    target: StoryState
    expected_version: Annotated[int,Field(ge=1)]
    caller_scope: Nonempty
    idempotency_key: Nonempty
    artifact: ArtifactRef | None = None
    manifest: ReviewManifest | None = None
    reason_code: Nonempty = 'stage_complete'
    trace_id: str | None = None

class StorySnapshot(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    story_id: UUID
    run_id: UUID
    state: StoryState
    version: Annotated[int,Field(ge=1)]
    artifacts: dict[str,ArtifactRef]
    manifest: ReviewManifest | None = None
    manifest_hash: Sha256 | None = None
    approval_id: str | None = None
    resume_target: StoryState | None = None
    failed_stage: str | None = None
    reason_code: str | None = None

class StateEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: UUID
    run_id: UUID
    story_id: UUID
    from_state: StoryState | None
    to_state: StoryState
    actor_type: str = 'service'
    actor_id: Nonempty
    reason_code: Nonempty
    timestamp: UtcDatetime
    trace_id: str | None = None
    version: Annotated[int,Field(ge=1)]
    input_hash: Sha256
    manifest_hash: Sha256 | None = None
    idempotency_key: Nonempty
    operation: Nonempty

class StateService:
    def __init__(self,conn: sqlite3.Connection,store: ArtifactStore,policy: PolicyEngine,*,clock: Callable[[],datetime] | None=None):
        if store.conn is not conn or policy.store is not store:
            raise ValueError('state and policy must share the transaction-bound store')
        self.conn=conn; self.store=store; self.policy=policy
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def get(self,story_id: UUID) -> StorySnapshot:
        row=self.conn.execute('SELECT payload,state,version,approval_id,resume_target FROM stories WHERE story_id=?',(str(story_id),)).fetchone()
        if row is None: raise StateConflict('story absent')
        data=json.loads(row[0]); data.update(state=row[1],version=row[2],approval_id=row[3],resume_target=row[4])
        return StorySnapshot.model_validate_json(json.dumps(data))

    def events(self,story_id: UUID) -> tuple[StateEvent,...]:
        return tuple(StateEvent.model_validate_json(row[0]) for row in self.conn.execute('SELECT payload FROM state_events WHERE story_id=? ORDER BY version',(str(story_id),)))

    def _begin(self):
        if self.conn.in_transaction: raise ValueError('state mutation requires idle connection')
        self.conn.execute('BEGIN IMMEDIATE')

    def _receipt(self,scope,operation,key,fingerprint):
        row=self.conn.execute('SELECT fingerprint,response FROM state_receipts WHERE caller_scope=? AND operation=? AND idempotency_key=?',(scope,operation,key)).fetchone()
        if row is None: return None
        if row[0]!=fingerprint: raise StateConflict('idempotency payload conflict')
        return StorySnapshot.model_validate_json(row[1])

    def _save(self,snapshot,old,scope,operation,key,fingerprint,reason,trace=None):
        body=snapshot.model_dump_json()
        if old is None:
            self.conn.execute('INSERT INTO stories(story_id,run_id,state,version,payload,manifest_hash,approval_id,resume_target) VALUES (?,?,?,?,?,?,?,?)',(str(snapshot.story_id),str(snapshot.run_id),snapshot.state.value,snapshot.version,body,snapshot.manifest_hash,snapshot.approval_id,snapshot.resume_target.value if snapshot.resume_target else None))
        else:
            result=self.conn.execute('UPDATE stories SET state=?,version=?,payload=?,manifest_hash=?,approval_id=?,resume_target=? WHERE story_id=? AND version=?',(snapshot.state.value,snapshot.version,body,snapshot.manifest_hash,snapshot.approval_id,snapshot.resume_target.value if snapshot.resume_target else None,str(snapshot.story_id),old.version))
            if result.rowcount!=1: raise StateConflict('version conflict')
        event=StateEvent(event_id=uuid4(),run_id=snapshot.run_id,story_id=snapshot.story_id,from_state=old.state if old else None,to_state=snapshot.state,actor_id=scope,reason_code=reason,timestamp=self.clock(),trace_id=trace,version=snapshot.version,input_hash=fingerprint,manifest_hash=snapshot.manifest_hash,idempotency_key=key,operation=operation)
        self.conn.execute('INSERT INTO state_events(event_id,story_id,version,reason_code,payload) VALUES (?,?,?,?,?)',(str(event.event_id),str(snapshot.story_id),snapshot.version,reason,event.model_dump_json()))
        self.conn.execute('INSERT INTO state_receipts VALUES (?,?,?,?,?)',(scope,operation,key,fingerprint,body))
        return snapshot

    @staticmethod
    def _fingerprint(data):
        return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def _valid(self,stage,ref,kind=None):
        result=self.policy.validate(stage,ref,kind=kind)
        if not result.report.ready: raise PolicyDenied('invalid '+stage+' artifact')
        return result

    def create(self,story_id: UUID,run_id: UUID,candidate: ArtifactRef,caller_scope: str,idempotency_key: str,expected_version: int=0) -> StorySnapshot:
        # Validate public values before hashing (model_construct is not authority).
        candidate=ArtifactRef.model_validate(candidate.model_dump())
        if not isinstance(story_id,UUID) or not isinstance(run_id,UUID) or not caller_scope or not idempotency_key: raise ValueError('invalid creation identifiers')
        fingerprint=self._fingerprint(dict(story_id=str(story_id),run_id=str(run_id),candidate=candidate.model_dump(),expected_version=expected_version))
        self._begin()
        try:
            replay=self._receipt(caller_scope,'create',idempotency_key,fingerprint)
            if replay is not None: self.conn.commit(); return replay
            if expected_version!=0 or self.conn.execute('SELECT 1 FROM stories WHERE story_id=?',(str(story_id),)).fetchone(): raise StateConflict('create requires absent story and version zero')
            self._valid('candidate',candidate)
            snapshot=StorySnapshot(story_id=story_id,run_id=run_id,state=StoryState.DISCOVERED,version=1,artifacts={'candidate':candidate})
            result=self._save(snapshot,None,caller_scope,'create',idempotency_key,fingerprint,'candidate_created')
            self.conn.commit(); return result
        except BaseException: self.conn.rollback(); raise

    def _retain(self,artifacts,target):
        return {name:ref for name,ref in artifacts.items() if name in STAGES[:NORMAL.index(target)+1]}

    def _revalidate_retained(self,artifacts):
        candidate=self._valid('candidate',artifacts['candidate'])
        return {stage:self._valid(stage,ref,kind=candidate.kind) for stage,ref in artifacts.items()}

    def _stage_safety(self,target,artifacts):
        values=self._revalidate_retained(artifacts)
        if target==StoryState.RESEARCHED:
            score=values.get('score')
            if score is None or score.decision!='ADVANCE' or not score.eligible: raise PolicyDenied('research requires eligible ADVANCE')
        if target==StoryState.RENDERED:
            for value in values.values():
                for license in value.licenses:
                    if license.rights=='unknown' or (license.expires_at is not None and license.expires_at<=self.clock()):
                        raise PolicyDenied('invalid or expired asset rights')
        dossier=values.get('dossier'); verification=values.get('verification')
        claims=list(dossier.claims) if dossier else []
        sources=list(dossier.sources) if dossier else []
        verdicts=list(verification.verdicts) if verification else []
        has_evidence=bool(claims or sources or verdicts)
        # Empty synthetic placeholders are harness artifacts, never editorial evidence.
        if target in {StoryState.VERIFIED,StoryState.SCRIPTED,StoryState.RENDERED} and has_evidence:
            if not validate_evidence(claims,sources,verdicts).ready: raise PolicyDenied('unsupported or stale claims')
        for stage,value in values.items():
            required=stage=='script' and target in {StoryState.SCRIPTED,StoryState.RENDERED} and has_evidence
            if (value.uses or required) and not validate_claim_uses(list(value.uses),claims,verdicts).ready:
                raise PolicyDenied('unsupported '+stage+' claims')

    def transition(self,request: TransitionRequest) -> StorySnapshot:
        request=TransitionRequest.model_validate(request.model_dump())
        fingerprint=request.record_hash()
        if self.conn.in_transaction: raise ValueError('state mutation requires idle connection')
        replay=self._receipt(request.caller_scope,'transition',request.idempotency_key,fingerprint)
        if replay is not None: return replay
        # Manifest registration is content-addressed and precedes the state txn.
        if request.manifest is not None:
            self.store.put(request.manifest.canonical_bytes(),'application/json','1.0')
        fingerprint=request.record_hash()
        self._begin()
        try:
            replay=self._receipt(request.caller_scope,'transition',request.idempotency_key,fingerprint)
            if replay is not None: self.conn.commit(); return replay
            old=self.get(request.story_id)
            if old.version!=request.expected_version: raise StateConflict('version conflict')
            target=request.target
            if target in {StoryState.APPROVED_FOR_PUBLISH,StoryState.PUBLISHED,StoryState.ANALYZED}: raise PolicyDenied('approval and publishing unavailable')
            if not allowed_edge(old.state,target): raise PolicyDenied('forbidden state edge')
            if target==StoryState.AWAITING_APPROVAL and old.state in NORMAL[7:9]: raise PolicyDenied('use revise for new review copy')
            artifacts=dict(old.artifacts); resume=None; failed=None; manifest=old.manifest; manifest_hash=old.manifest_hash
            if target in {StoryState.NEEDS_RESEARCH,StoryState.NEEDS_REVISION}:
                failed=old.state.value
                if target==StoryState.NEEDS_RESEARCH:
                    resume=StoryState.DISCOVERED
                    for checkpoint,stage in ((StoryState.CLUSTERED,'cluster'),(StoryState.SCORED,'score')):
                        if stage not in artifacts or not self.policy.validate(stage,artifacts[stage]).report.ready: break
                        resume=checkpoint
                else: resume=old.state if old.state in NORMAL[:7] else StoryState.RENDERED
                artifacts=self._retain(artifacts,resume)
            elif old.state in {StoryState.NEEDS_RESEARCH,StoryState.NEEDS_REVISION} and target!=StoryState.REJECTED:
                if target!=old.resume_target: raise PolicyDenied('only saved checkpoint may resume')
                if request.artifact is not None:
                    kind=self._valid('candidate',old.artifacts['candidate']).kind
                    self._valid(STAGE_FOR[target],request.artifact,kind=kind)
                    artifacts[STAGE_FOR[target]]=request.artifact
                artifacts=self._retain(artifacts,target)
                self._stage_safety(target,artifacts)
            elif target==StoryState.AWAITING_APPROVAL:
                manifest=request.manifest
                if manifest is None or manifest.story_id!=old.story_id or manifest.kind!=self._valid('candidate',artifacts['candidate']).kind or not self.policy.evaluate_readiness(manifest,self.clock()).ready: raise PolicyDenied('manifest not ready')
                # Manifest members must equal the retained current stage outputs.
                for stage in ('dossier','verification','script','render'):
                    if artifacts.get(stage)!=getattr(manifest,stage): raise PolicyDenied('manifest differs from current stage')
                manifest_hash=manifest.record_hash()
            elif target!=StoryState.REJECTED:
                stage=STAGE_FOR[target]
                if request.artifact is None: raise PolicyDenied('stage artifact required')
                artifacts[stage]=request.artifact
                self._stage_safety(target,artifacts)
            result=old.model_copy(update=dict(state=target,version=old.version+1,artifacts=artifacts,manifest=manifest,manifest_hash=manifest_hash,approval_id=None,resume_target=resume,failed_stage=failed,reason_code=request.reason_code))
            self._save(result,old,request.caller_scope,'transition',request.idempotency_key,fingerprint,request.reason_code,request.trace_id)
            self.conn.commit(); return result
        except BaseException: self.conn.rollback(); raise

    def revise(self,story_id: UUID,manifest: ReviewManifest,change: ChangeKind,expected_version: int,idempotency_key: str,*,caller_scope: str='review') -> StorySnapshot:
        manifest=ReviewManifest.model_validate(manifest.model_dump())
        if not isinstance(change,ChangeKind): raise ValueError('invalid change kind')
        fingerprint=self._fingerprint(dict(story_id=str(story_id),manifest=manifest.model_dump(mode='json'),change=change.value,expected_version=expected_version))
        if self.conn.in_transaction: raise ValueError('state mutation requires idle connection')
        replay=self._receipt(caller_scope,'revise',idempotency_key,fingerprint)
        if replay is not None: return replay
        self.store.put(manifest.canonical_bytes(),'application/json','1.0')
        self._begin()
        try:
            replay=self._receipt(caller_scope,'revise',idempotency_key,fingerprint)
            if replay is not None: self.conn.commit(); return replay
            old=self.get(story_id)
            if old.version!=expected_version: raise StateConflict('version conflict')
            if old.state not in NORMAL or old.manifest is None: raise PolicyDenied('no revisable review manifest')
            if manifest.kind!=old.manifest.kind: raise PolicyDenied('story kind cannot change')
            if manifest.story_id!=story_id or manifest.revision!=old.manifest.revision+1: raise PolicyDenied('manifest identity/revision mismatch')
            for name in MEMBERS: self.store.read(getattr(manifest,name))
            changed={name for name in MEMBERS if getattr(manifest,name).sha256!=getattr(old.manifest,name).sha256}
            evidence_changed=bool(changed & {'dossier','verification'}) or manifest.claim_revision_refs!=old.manifest.claim_revision_refs or manifest.source_revision_refs!=old.manifest.source_revision_refs
            if evidence_changed: resume=StoryState.SCORED
            elif changed & {'script','captions','subtitles','charts'}: resume=StoryState.RESEARCHED
            elif changed: resume=StoryState.SCRIPTED
            else: resume=None
            if resume is None:
                if old.state not in NORMAL[7:9] or not self.policy.evaluate_readiness(manifest,self.clock()).ready: raise PolicyDenied('review copy not ready')
                target=StoryState.AWAITING_APPROVAL; artifacts=dict(old.artifacts)
            else:
                target=StoryState.NEEDS_REVISION; artifacts=self._retain(old.artifacts,resume)
            result=old.model_copy(update=dict(state=target,version=old.version+1,manifest=manifest,manifest_hash=manifest.record_hash(),approval_id=None,resume_target=resume,artifacts=artifacts,failed_stage=old.state.value,reason_code='manifest_revised'))
            self._save(result,old,caller_scope,'revise',idempotency_key,fingerprint,'manifest_revised')
            self.conn.commit(); return result
        except BaseException: self.conn.rollback(); raise

    def _review_transition(self,old,decision,scope,key,fingerprint,trace_attempt):
        """Internal seam: caller owns the authenticated decision transaction."""
        if not self.conn.in_transaction: raise ValueError('review transaction required')
        target={'approve':StoryState.APPROVED_FOR_PUBLISH,'reject':StoryState.REJECTED,'revise':StoryState.NEEDS_REVISION}[decision.action]
        if not allowed_edge(old.state,target): raise PolicyDenied('forbidden review edge')
        approval=None; resume=None; artifacts=dict(old.artifacts)
        if decision.action=='approve':
            if old.state!=StoryState.AWAITING_APPROVAL or old.manifest is None or not self.policy.evaluate_readiness(old.manifest,self.clock()).ready:
                raise PolicyDenied('current manifest not ready')
            if old.manifest.record_hash()!=old.manifest_hash: raise PolicyDenied('manifest integrity')
            canonical=old.manifest.canonical_bytes()
            manifest_ref=ArtifactRef(sha256=old.manifest_hash,media_type='application/json',
                size_bytes=len(canonical),schema_version=old.manifest.schema_version)
            try:
                if self.store.read(manifest_ref)!=canonical: raise ValueError('manifest differs')
            except ValueError:
                raise PolicyDenied('registered manifest unavailable or corrupt') from None
            for stage in ('dossier','verification','script','render'):
                if old.artifacts.get(stage)!=getattr(old.manifest,stage): raise PolicyDenied('manifest differs from current stage')
            approval=str(decision.decision_id)
            self.conn.execute('INSERT INTO approval_decisions VALUES (?,?,?,?,?)',(approval,str(old.story_id),old.manifest_hash,old.version,scope))
        elif decision.action=='revise':
            resume=old.state if old.state in NORMAL[:7] else StoryState.RENDERED
            artifacts=self._retain(artifacts,resume)
        result=old.model_copy(update=dict(state=target,version=old.version+1,approval_id=approval,resume_target=resume,artifacts=artifacts,reason_code='human_'+decision.action))
        trace_id = trace_attempt(decision.decision_id)
        self._save(result,old,scope,'human_review',key,fingerprint,'human_'+decision.action,trace_id)
        return trace_id
