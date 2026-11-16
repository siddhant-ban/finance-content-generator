"""Human decisions under the same write lock as manifest, policy and session checks."""
from typing import Literal
from uuid import UUID,uuid4
from pydantic import Field, model_validator
from app.contracts.common import StrictModel,Sha256,UtcDatetime,Principal
from app.observability.models import TraceID
from app.workflow.state import StateConflict,PolicyDenied
from app.release.service import ReleaseService

class ReviewDenied(PolicyDenied):
    """Current authenticated reviewer is required."""

class HumanDecision(StrictModel):
    schema_version: Literal['1.0','1.1']='1.1'
    decision_id: UUID
    story_id: UUID
    reviewer_provider: str
    reviewer_id: int
    action: Literal['approve','reject','revise']
    decided_at: UtcDatetime
    manifest_hash: Sha256
    expected_version: int = Field(ge=1)
    notes: str = Field(max_length=8000)
    trace_id: TraceID | None
    trace_status: Literal['RECORDED','DEGRADED','LEGACY_UNVERIFIED'] = 'LEGACY_UNVERIFIED'

    @model_validator(mode='after')
    def verify_trace_status(self):
        if self.schema_version == '1.0':
            if self.trace_status != 'LEGACY_UNVERIFIED':
                raise ValueError('invalid_legacy_trace_status')
        elif self.trace_status == 'LEGACY_UNVERIFIED' or (self.trace_status == 'RECORDED') != (self.trace_id is not None):
            raise ValueError('inconsistent_trace_status')
        return self


class ReviewService:
    def __init__(self,state,*,current_session_verifier=None,releases=None,telemetry=None):
        self.state=state; self.conn=state.conn
        self.telemetry = telemetry
        self.verify=current_session_verifier or (lambda principal:False)
        self.releases=releases or ReleaseService(self.conn,current_session_verifier=self.verify,clock=state.clock)
        if self.releases.conn is not self.conn: raise ValueError('shared review database required')

    def _human(self,principal):
        try:
            value=Principal.model_validate_json(principal.model_dump_json())
            if value.kind!='human' or self.verify(value) is not True: raise ValueError()
            return value
        except Exception:
            raise ReviewDenied('current_authenticated_allowed_human_required') from None

    def decide(self,story_id: UUID,decision: Literal['approve','reject','revise'],manifest_hash: str,
               expected_version: int,idempotency_key: str,principal: Principal,notes: str) -> HumanDecision:
        principal=self._human(principal)
        receipt=HumanDecision(decision_id=uuid4(),story_id=story_id,reviewer_provider=principal.provider,
            reviewer_id=principal.account_id,action=decision,decided_at=self.state.clock(),manifest_hash=manifest_hash,
            expected_version=expected_version,notes=notes,trace_id=None,trace_status='DEGRADED')
        if not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=200: raise StateConflict('invalid_idempotency_key')
        scope=self.state._fingerprint([principal.provider,principal.account_id,'human_review'])
        fingerprint=self.state._fingerprint([str(story_id),decision,manifest_hash,expected_version,notes])
        self.state._begin()
        try:
            self._human(principal)
            row=self.conn.execute('SELECT fingerprint,decision_id FROM review_receipts WHERE scope=? AND idempotency_key=?',(scope,idempotency_key)).fetchone()
            if row:
                if row[0]!=fingerprint: raise StateConflict('idempotency payload conflict')
                result=HumanDecision.model_validate_json(self.conn.execute('SELECT payload FROM human_decisions WHERE decision_id=?',(row[1],)).fetchone()[0])
                self.conn.commit(); return result
            old=self.state.get(story_id)
            if old.version!=expected_version or old.manifest_hash!=manifest_hash: raise StateConflict('manifest or version conflict')
            trace_id = self.state._review_transition(old,receipt,scope,idempotency_key,fingerprint,self._trace_attempt)
            if trace_id is not None:
                receipt = receipt.model_copy(update={'trace_id':trace_id,'trace_status':'RECORDED'})
            self.conn.execute('INSERT INTO human_decisions VALUES (?,?,?)',(str(receipt.decision_id),str(story_id),receipt.model_dump_json()))
            self.conn.execute('INSERT INTO review_receipts VALUES (?,?,?,?)',(scope,idempotency_key,fingerprint,str(receipt.decision_id)))
            self._human(principal)
            self.conn.commit()
            return receipt
        except BaseException:
            self.conn.rollback(); raise

    def _trace_attempt(self, decision_id):
        try:
            if self.telemetry is None:
                from app.observability.tracing import Telemetry
                self.telemetry = Telemetry(self.state.store.root.parent/'telemetry.db')
            return self.telemetry.decision_attempt(decision_id)
        except Exception:
            return None

    def release_decide(self,candidate_hash: str,expected_version: int,bootstrap: bool,
                       principal: Principal,idempotency_key: str):
        self._human(principal)
        return self.releases.approve(candidate_hash,principal,expected_version,bootstrap,idempotency_key)
