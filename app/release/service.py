"""Transactional release registry. Principal values alone confer no authority."""
from datetime import datetime,timezone
import json
import sqlite3
from uuid import uuid4
from app.contracts.common import Principal
from app.evals.models import ExperimentBundle
from app.evals.run import canonical,digest,validate_bundle
from app.release.check import intrinsic,baseline_reasons
from app.release.models import (ReleaseCandidate,GateDecision,ReleaseApproval,
    FeedbackAnnotation,FeedbackCandidate)

class ReleaseConflict(ValueError):
    """Optimistic version or scoped idempotency payload changed."""

class ReleaseDenied(ValueError):
    """No current allowed human session, or insufficient release evidence."""

class ReleaseService:
    def __init__(self,conn: sqlite3.Connection,*,current_session_verifier=None,clock=None):
        self.conn=conn
        self.current_session_verifier=current_session_verifier or (lambda principal:False)
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def _human(self,principal):
        try:
            principal=Principal.model_validate_json(principal.model_dump_json())
            if principal.kind!='human' or not principal.session_id or self.current_session_verifier(principal) is not True:
                raise ValueError()
            return principal
        except Exception:
            raise ReleaseDenied('current_authenticated_allowed_human_required') from None

    def _head(self,agent):
        return self.conn.execute('SELECT version,active_id,baseline_id FROM release_heads WHERE agent_name=?',(agent,)).fetchone()

    def _receipt(self,approval_id):
        row=self.conn.execute('SELECT receipt FROM release_approvals WHERE approval_id=?',(approval_id,)).fetchone()
        return None if row is None else ReleaseApproval.model_validate_json(row[0])

    def active(self,agent_name: str) -> ReleaseApproval | None:
        head=self._head(agent_name)
        return self._receipt(head[1]) if head and head[1] else None

    def approved_baseline(self,agent_name: str,split: str) -> ExperimentBundle:
        """Return pinned historical evidence, including after execution revocation."""
        head=self._head(agent_name)
        if not head:
            raise ReleaseDenied('human_approved_baseline_required')
        receipt=self._receipt(head[2])
        row=self.conn.execute('SELECT evidence FROM release_candidates WHERE candidate_hash=?',(receipt.candidate_hash,)).fetchone()
        bundle=validate_bundle(ExperimentBundle.model_validate_json(json.dumps(json.loads(row[0])[split])))
        if bundle.record_hash()!=receipt.experiment_hashes[split]:
            raise ReleaseDenied('baseline_evidence_integrity')
        return bundle

    def _evaluate(self,candidate):
        candidate=ReleaseCandidate.model_validate_json(candidate.model_dump_json())
        identity=candidate.record_hash()
        head=self._head(candidate.agent_name)
        version=head[0] if head else 0
        baseline=self._receipt(head[2]) if head else None
        base=dict(candidate_hash=identity,current_version=version,baseline_hash=baseline.candidate_hash if baseline else None)
        try:
            bundles,reasons=intrinsic(candidate)
        except Exception:
            return GateDecision(**base,status='ERROR',reasons=('invalid_evidence_or_configuration',)),None
        if reasons:
            return GateDecision(**base,status='BLOCKED',reasons=reasons),bundles
        if baseline:
            try:
                previous={split:self.approved_baseline(candidate.agent_name,split) for split in bundles}
                reasons=baseline_reasons(bundles,previous)
            except Exception:
                reasons=('invalid_baseline',)
            if reasons:
                return GateDecision(**base,status='BLOCKED',reasons=reasons),bundles
            active=self.active(candidate.agent_name)
            status='APPROVED' if active and active.candidate_hash==identity else 'ELIGIBLE'
        else:
            # A corrupt/deleted head must not reopen bootstrap after prior approval.
            history=self.conn.execute('SELECT 1 FROM release_approvals WHERE agent_name=? LIMIT 1',(candidate.agent_name,)).fetchone()
            if history:
                return GateDecision(**base,status='BLOCKED',reasons=('historical_baseline_unavailable',)),bundles
            status='REQUIRES_BOOTSTRAP_APPROVAL'
        return GateDecision(**base,status=status),bundles

    def check(self,candidate: ReleaseCandidate) -> GateDecision:
        """Readiness is never approval. Persist only complete validated evidence."""
        if self.conn.in_transaction:
            raise ReleaseConflict('idle_connection_required')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            decision,bundles=self._evaluate(candidate)
            if decision.status in {'APPROVED','ELIGIBLE','REQUIRES_BOOTSTRAP_APPROVAL'}:
                evidence=canonical({split:bundle.model_dump(mode='json') for split,bundle in bundles.items()}).decode()
                stored=self.conn.execute('SELECT candidate,evidence FROM release_candidates WHERE candidate_hash=?',
                    (candidate.record_hash(),)).fetchone()
                expected=(candidate.canonical_bytes().decode(),evidence)
                if stored is None:
                    self.conn.execute('INSERT INTO release_candidates VALUES (?,?,?)',
                        (candidate.record_hash(),*expected))
                elif stored!=expected:
                    raise ReleaseConflict('immutable_candidate_conflict')
            self.conn.commit()
            return decision
        except BaseException:
            self.conn.rollback(); raise

    def approve(self,candidate_hash: str,principal: Principal,expected_version: int,
                bootstrap: bool,idempotency_key: str) -> ReleaseApproval:
        principal=self._human(principal)  # Even historical replay requires a live session.
        if type(expected_version) is not int or expected_version<0 or type(bootstrap) is not bool or not idempotency_key:
            raise ReleaseConflict('invalid_approval_request')
        if self.conn.in_transaction:
            raise ReleaseConflict('idle_connection_required')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            self._human(principal)  # A session may expire while acquiring the write lock.
            scope=digest(canonical([principal.provider,principal.account_id,'release_approval']))
            # Candidate hash binds the full manifest, configuration and experiment pins.
            fingerprint=digest(canonical([candidate_hash,expected_version,bootstrap]))
            replay=self.conn.execute('SELECT fingerprint,approval_id FROM release_receipts WHERE scope=? AND idempotency_key=?',
                (scope,idempotency_key)).fetchone()
            if replay:
                if replay[0]!=fingerprint:
                    raise ReleaseConflict('idempotency_payload_conflict')
                receipt=self._receipt(replay[1])
                self.conn.commit()
                return receipt
            row=self.conn.execute('SELECT candidate FROM release_candidates WHERE candidate_hash=?',(candidate_hash,)).fetchone()
            if row is None:
                raise ReleaseDenied('candidate_not_eligible')
            candidate=ReleaseCandidate.model_validate_json(row[0])
            decision,bundles=self._evaluate(candidate)
            if decision.current_version!=expected_version:
                raise ReleaseConflict('release_version_conflict')
            if ((decision.status=='REQUIRES_BOOTSTRAP_APPROVAL' and not bootstrap)
                    or (decision.status in {'ELIGIBLE','APPROVED'} and bootstrap)
                    or decision.status not in {'REQUIRES_BOOTSTRAP_APPROVAL','ELIGIBLE'}):
                raise ReleaseDenied('release_not_eligible_for_requested_approval')
            self._human(principal)  # Recheck immediately before the mutation.
            receipt=ReleaseApproval(approval_id=str(uuid4()),candidate_hash=candidate_hash,
                release_pins={**bundles['dev'].provenance,
                    'agent_version':bundles['dev'].records[0].output.agent_version,
                    'prompt_id':bundles['dev'].records[0].output.prompt_id,
                    'config_version':bundles['dev'].records[0].output.config_version},
                experiment_hashes={split:bundle.record_hash() for split,bundle in bundles.items()},
                policy_hash=candidate.policy.record_hash(),baseline_hash=decision.baseline_hash,
                version=expected_version+1,bootstrap=bootstrap,approved_at=self.clock())
            self.conn.execute('INSERT INTO release_approvals VALUES (?,?,?,?,?)',
                (receipt.approval_id,receipt.agent_name,candidate_hash,receipt.version,receipt.model_dump_json()))
            self.conn.execute('INSERT INTO release_heads VALUES (?,?,?,?) ON CONFLICT(agent_name) DO UPDATE SET version=excluded.version,active_id=excluded.active_id,baseline_id=excluded.baseline_id',
                (receipt.agent_name,receipt.version,receipt.approval_id,receipt.approval_id))
            self.conn.execute('INSERT INTO release_receipts VALUES (?,?,?,?)',(scope,idempotency_key,fingerprint,receipt.approval_id))
            self.conn.execute('INSERT INTO release_reviewers VALUES (?,?,?)',
                (receipt.approval_id,principal.provider,principal.account_id))
            self._event('approve',receipt.agent_name,receipt.version,candidate_hash)
            self.conn.commit()
            return receipt
        except BaseException:
            self.conn.rollback(); raise

    def _event(self,operation,agent,version,candidate_hash):
        # Private reviewer/session identifiers are deliberately absent from telemetry.
        self.conn.execute('INSERT INTO release_events VALUES (?,?,?,?,?,?)',
            (str(uuid4()),operation,agent,version,candidate_hash,self.clock().isoformat()))

    def revoke(self,agent_name: str,principal: Principal,expected_version: int) -> None:
        self._human(principal)
        if self.conn.in_transaction:
            raise ReleaseConflict('idle_connection_required')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            head=self._head(agent_name)
            if not head or head[0]!=expected_version or head[1] is None:
                raise ReleaseConflict('release_version_conflict')
            receipt=self._receipt(head[1]); self._human(principal)
            self.conn.execute('UPDATE release_heads SET version=?,active_id=NULL WHERE agent_name=?',(expected_version+1,agent_name))
            self._event('revoke',agent_name,expected_version+1,receipt.candidate_hash)
            self.conn.commit()
        except BaseException:
            self.conn.rollback(); raise

    def require_approved(self,agent_name: str,release_pins: dict,*,validators_ready: bool,
                         execution_kind: str='production') -> ReleaseApproval:
        """Future execution seam; synthetic approvals can only authorize a foundation demo.

        This does not change AgentRunner's separate default-deny execution policy.
        """
        receipt=self.active(agent_name)
        if (receipt is None or validators_ready is not True or release_pins!=receipt.release_pins
                or execution_kind!='foundation_demo' or agent_name!='foundation'):
            raise ReleaseDenied('approved_exact_release_and_live_validators_required')
        row=self.conn.execute('SELECT candidate FROM release_candidates WHERE candidate_hash=?',(receipt.candidate_hash,)).fetchone()
        decision,_=self._evaluate(ReleaseCandidate.model_validate_json(row[0]))
        if decision.status!='APPROVED':
            raise ReleaseDenied('release_evidence_changed')
        return receipt

    def queue_feedback(self,annotation: FeedbackAnnotation) -> FeedbackCandidate:
        annotation=FeedbackAnnotation.model_validate_json(annotation.model_dump_json())
        from pathlib import Path
        path=Path(annotation.annotation_ref)
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes())!=annotation.annotation_hash:
            raise ReleaseDenied('local_annotation_integrity')
        # An annotation cannot approve its own privacy/rights/adjudication review.
        annotation=annotation.model_copy(update={'privacy_review':'PENDING','rights_review':'PENDING','adjudication_review':'PENDING'})
        identity=annotation.record_hash()
        if self.conn.in_transaction:
            raise ReleaseConflict('idle_connection_required')
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row=self.conn.execute('SELECT receipt FROM feedback_quarantine WHERE feedback_id=?',(identity,)).fetchone()
            if row is None:
                receipt=FeedbackCandidate(feedback_id=identity,annotation=annotation,queued_at=self.clock()).model_dump_json()
                self.conn.execute('INSERT INTO feedback_quarantine VALUES (?,?)',(identity,receipt))
            else:
                receipt=row[0]
            self.conn.commit()
            return FeedbackCandidate.model_validate_json(receipt)
        except BaseException:
            self.conn.rollback(); raise
