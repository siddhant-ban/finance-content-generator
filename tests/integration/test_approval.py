"""Real synthetic artifacts and server sessions; no browser actor authority."""
from datetime import timedelta
import sqlite3
import json
import pytest
from app.auth.sessions import SessionService
from app.auth.github import GitHubIdentity
from tests.unit.test_github_auth import configured
from tests.integration.task4_fixtures import setup,awaiting,NOW,artifact

@pytest.fixture
def review(tmp_path):
    from app.review.service import ReviewService
    conn,store,engine,state=setup(tmp_path)
    sessions=SessionService(conn,configured(),clock=lambda:NOW)
    cookie=sessions.create(GitHubIdentity(account_id=123,account_type='User',display_login='reviewer'))
    principal=sessions.require(cookie.value,NOW)
    service=ReviewService(state,current_session_verifier=sessions.verify_principal)
    snapshot,manifest=awaiting(state,store)
    yield service,state,sessions,principal,cookie,snapshot,manifest
    conn.close()

def decide(review,**changes):
    service,_,_,principal,_,s,_=review
    values=dict(story_id=s.story_id,decision='approve',manifest_hash=s.manifest_hash,expected_version=s.version,idempotency_key='form-1',principal=principal,notes='human notes')
    return service.decide(**(values|changes))

def test_current_human_exact_manifest_can_approve(review):
    result=decide(review)
    assert result.action=='approve' and result.reviewer_id==123
    state=review[1].get(review[5].story_id)
    assert state.state.value=='APPROVED_FOR_PUBLISH' and state.approval_id==str(result.decision_id)
    assert len(review[1].events(state.story_id))==9
    assert decide(review)==result
    assert len(review[1].events(state.story_id))==9

@pytest.mark.parametrize('mutation',[{'kind':'agent'},{'account_id':456},{'provider':'forged'}])
def test_anonymous_agent_forged_or_expired_session_cannot_approve(review,mutation):
    from app.review.service import ReviewDenied
    with pytest.raises(ReviewDenied): decide(review,principal=review[3].model_copy(update=mutation))
    review[2].logout(review[4].value)
    with pytest.raises(ReviewDenied): decide(review)

@pytest.mark.parametrize('changes',[{'manifest_hash':'0'*64},{'expected_version':7}])
def test_stale_hash_or_version_conflicts(review,changes):
    from app.workflow.state import StateConflict
    with pytest.raises(StateConflict): decide(review,**changes)
    assert review[1].get(review[5].story_id).state.value=='AWAITING_APPROVAL'

def test_historical_replay_never_reactivates_approval(review):
    from app.contracts.workflow import ChangeKind
    receipt=decide(review)
    s=review[1].get(review[5].story_id)
    m=review[6].model_copy(update={'revision':2,'render':artifact(review[1].store,'render',changed=True)})
    review[1].revise(s.story_id,m,ChangeKind.VISUAL,s.version,'edit')
    assert decide(review)==receipt
    assert review[1].get(s.story_id).approval_id is None
    assert review[1].get(s.story_id).state.value=='NEEDS_REVISION'

def test_same_key_changed_payload_conflicts(review):
    from app.workflow.state import StateConflict
    decide(review)
    with pytest.raises(StateConflict): decide(review,notes='different')

@pytest.mark.parametrize('table',['artifacts','stories','approval_decisions','state_events','state_receipts'])
@pytest.mark.parametrize('alias',['rowid','_rowid_','oid'])
def test_hidden_replacement_aliases_closed(tmp_path,table,alias):
    conn,store,_,state=setup(tmp_path)
    s=state.create(__import__('uuid').uuid4(),__import__('uuid').uuid4(),artifact(store,'candidate'),'test','create')
    before=tuple(conn.execute(f'SELECT * FROM {table}'))
    with pytest.raises(sqlite3.OperationalError,match='no column named'):
        conn.execute(f'INSERT OR REPLACE INTO {table} ({alias}) VALUES (1)')
    assert tuple(conn.execute(f'SELECT * FROM {table}'))==before
    conn.close()


def test_expired_license_or_missing_evidence_blocks(review):
    from app.workflow.state import PolicyDenied
    service,state,sessions,principal,cookie,s,m=review
    # Time passes after awaiting; rights are rechecked at the final decision.
    from app.contracts.workflow import ChangeKind
    rights=artifact(state.store,'asset_register',licenses=[{'asset_id':'synthetic','rights':'owned','expires_at':(NOW+timedelta(seconds=1)).isoformat()}])
    changed=m.model_copy(update={'revision':2,'asset_register':rights})
    gap=state.revise(s.story_id,changed,ChangeKind.VISUAL,s.version,'rights')
    from app.workflow.state import TransitionRequest
    from app.contracts.workflow import StoryState
    for target,ref,manifest_value in [(StoryState.SCRIPTED,None,None),(StoryState.RENDERED,changed.render,None),(StoryState.AWAITING_APPROVAL,None,changed)]:
        gap=state.transition(TransitionRequest(story_id=s.story_id,target=target,expected_version=gap.version,caller_scope='test',idempotency_key=target.value,artifact=ref,manifest=manifest_value))
    state.clock=lambda:NOW+timedelta(seconds=2)
    with pytest.raises(PolicyDenied):
        decide(review,manifest_hash=gap.manifest_hash,expected_version=gap.version,idempotency_key='expired')
    state.clock=lambda:NOW
    state.store._path(m.dossier).unlink()
    with pytest.raises(PolicyDenied):
        decide(review,manifest_hash=gap.manifest_hash,expected_version=gap.version,idempotency_key='missing')


def test_session_expiry_while_acquiring_lock_cannot_approve(review):
    from app.review.service import ReviewDenied
    svc,state,sessions,principal,cookie,s,m=review
    original=state._begin
    def expired_lock():
        sessions.logout(cookie.value)
        original()
    state._begin=expired_lock
    with pytest.raises(ReviewDenied): decide(review)
    assert state.get(s.story_id).approval_id is None
    assert state.conn.execute('SELECT count(*) FROM human_decisions').fetchone()[0]==0


def test_release_private_human_audit_is_atomic(browser,evidence,tmp_path):
    from tests.release_fixtures import candidate_for
    client,svc,_,_,principal,_=browser
    candidate=candidate_for(evidence,tmp_path/'bundles');svc.releases.check(candidate)
    receipt=svc.release_decide(candidate.record_hash(),0,True,principal,'audited')
    row=svc.conn.execute('SELECT reviewer_provider,reviewer_id FROM release_reviewers WHERE approval_id=?',(receipt.approval_id,)).fetchone()
    assert row==('github',123)

from tests.integration.test_review_ui import browser
from tests.release_fixtures import evidence


def test_concurrent_edit_and_approve_never_stale(review,tmp_path):
    import threading
    from app.storage.db import open_database
    from app.storage.artifacts import ArtifactStore
    from app.workflow.policy import PolicyEngine
    from app.workflow.state import StateService,StateConflict
    from app.contracts.workflow import ChangeKind
    from tests.integration.task4_fixtures import validator,STAGES
    from app.review.service import ReviewService
    svc,state,sessions,principal,cookie,s,m=review
    edited=m.model_copy(update={'revision':2,'render':artifact(state.store,'render',race=True)})
    barrier=threading.Barrier(2); outcomes=[]
    def compete(action):
        conn=open_database(tmp_path/'state.db')
        store=ArtifactStore(tmp_path/'artifacts',conn);policy=PolicyEngine(store)
        for stage in STAGES: policy.register(stage,'fixture.v1','synthetic_foundation',validator)
        local=StateService(conn,store,policy,clock=lambda:NOW)
        auth=SessionService(conn,configured(),clock=lambda:NOW)
        reviewer=ReviewService(local,current_session_verifier=auth.verify_principal)
        try:
            barrier.wait(timeout=3)
            if action=='approve': reviewer.decide(s.story_id,'approve',s.manifest_hash,s.version,'race',principal,'')
            else: local.revise(s.story_id,edited,ChangeKind.VISUAL,s.version,'race-edit')
            outcomes.append(action)
        except StateConflict: outcomes.append('conflict')
        finally: conn.close()
    threads=[threading.Thread(target=compete,args=(a,)) for a in ('approve','edit')]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=5)
    assert len(outcomes)==2 and outcomes.count('conflict')==1
    current=state.get(s.story_id)
    assert current.version==9 and len(state.events(s.story_id))==9
    assert (current.manifest_hash==s.manifest_hash and current.approval_id is not None) if 'approve' in outcomes else (current.manifest_hash==edited.record_hash() and current.approval_id is None)


@pytest.mark.parametrize('table',['human_decisions','review_receipts','release_reviewers'])
@pytest.mark.parametrize('alias',['rowid','_rowid_','oid'])
def test_review_audit_hidden_aliases_closed(review,table,alias):
    with pytest.raises(sqlite3.OperationalError,match='no column named'):
        review[1].conn.execute(f'INSERT OR REPLACE INTO {table} ({alias}) VALUES(1)')


def test_audit_failure_rolls_back_state_and_receipt(review):
    svc,state,_,_,_,s,_=review
    state.conn.execute("CREATE TRIGGER fail_review BEFORE INSERT ON human_decisions BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError): decide(review)
    assert state.get(s.story_id).state.value=='AWAITING_APPROVAL'
    assert len(state.events(s.story_id))==8
    assert state.conn.execute('SELECT count(*) FROM approval_decisions').fetchone()[0]==0
    assert state.conn.execute('SELECT count(*) FROM review_receipts').fetchone()[0]==0
    attempt,=svc.telemetry.pending_records()
    assert attempt.name=='approval.decision' and attempt.events[0].name=='human_decision'
    assert state.conn.execute('SELECT count(*) FROM human_decisions').fetchone()[0]==0


def test_review_trace_contains_only_opaque_decision_id(review):
    receipt=decide(review,notes='private-note-sentinel')
    assert decide(review,notes='private-note-sentinel')==receipt
    span,=review[0].telemetry.pending_records()
    assert span.trace_id==receipt.trace_id and span.name=='approval.decision'
    assert set(span.attributes)=={'latency_ms'}
    event,=span.events
    assert event.name=='human_decision' and event.attributes=={'decision_id':str(receipt.decision_id)}
    assert 'private-note-sentinel' not in span.model_dump_json()
    assert review[3].session_id not in span.model_dump_json()


def test_decision_survives_durable_span_failure(review,monkeypatch):
    import sqlite3
    def fail(*args): raise sqlite3.OperationalError('disk unavailable')
    from app.observability.outbox import Outbox
    monkeypatch.setattr(Outbox,'enqueue',fail)
    receipt=decide(review)
    assert receipt.trace_id is None and receipt.trace_status=='DEGRADED'
    assert review[1].get(receipt.story_id).approval_id==str(receipt.decision_id)
    assert decide(review)==receipt and not review[0].telemetry.pending_records()


@pytest.mark.parametrize('member,target',[('dossier','SCORED'),('verification','SCORED'),('script','RESEARCHED'),('captions','RESEARCHED'),('subtitles','RESEARCHED'),('charts','RESEARCHED'),('render','SCRIPTED'),('attributions','SCRIPTED'),('asset_register','SCRIPTED')])
def test_all_member_changes_revoke(review,member,target):
    from app.contracts.workflow import ChangeKind
    receipt=decide(review)
    svc,state,_,_,_,s,m=review
    current=state.get(s.story_id)
    changed=m.model_copy(update={'revision':2,member:artifact(state.store,member,edited=True)})
    result=state.revise(s.story_id,changed,ChangeKind.REVIEW_METADATA,current.version,'edit-member')
    assert result.approval_id is None and result.resume_target.value==target
    assert state.conn.execute('SELECT count(*) FROM human_decisions').fetchone()[0]==1
    assert state.conn.execute('SELECT count(*) FROM approval_decisions').fetchone()[0]==1
    assert decide(review)==receipt and state.get(s.story_id).approval_id is None


def test_expired_session_cannot_approve(review):
    from app.review.service import ReviewDenied
    review[2].clock=lambda:NOW+timedelta(seconds=1800)
    with pytest.raises(ReviewDenied):decide(review)
    assert review[1].get(review[5].story_id).approval_id is None


def test_new_immutable_audit_cannot_update_delete_replace(review):
    decide(review)
    conn=review[1].conn
    for table,column in [('human_decisions','payload'),('review_receipts','fingerprint')]:
        before=tuple(conn.execute(f'SELECT * FROM {table}'))
        for statement in [f'UPDATE {table} SET {column}={column}',f'DELETE FROM {table}',f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}']:
            with pytest.raises(sqlite3.IntegrityError):conn.execute(statement)
            conn.rollback()
            assert tuple(conn.execute(f'SELECT * FROM {table}'))==before


@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_registered_manifest_blob_required_for_approval(review,damage):
    from app.workflow.state import PolicyDenied
    svc,state,_,_,_,snapshot,manifest=review
    registered=state.store._registered(snapshot.manifest_hash)
    path=state.store._path(registered)
    if damage=='missing': path.unlink()
    else: path.write_bytes(b'corrupted registered manifest')
    before=tuple(state.events(snapshot.story_id))
    before_receipts=tuple(state.conn.execute('SELECT * FROM state_receipts'))
    with pytest.raises(PolicyDenied): decide(review)
    assert state.get(snapshot.story_id)==snapshot
    assert state.events(snapshot.story_id)==before
    assert tuple(state.conn.execute('SELECT * FROM state_receipts'))==before_receipts
    for table in ('approval_decisions','human_decisions','review_receipts'):
        assert state.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0
    assert not state.conn.in_transaction


def test_receipt_trace_contract_marks_historical_link_unverified(review):
    from app.review.service import HumanDecision
    from pydantic import ValidationError
    payload=decide(review).model_dump(mode='json')
    legacy={**payload,'schema_version':'1.0'}
    del legacy['trace_status']
    assert HumanDecision.model_validate_json(json.dumps(legacy)).trace_status=='LEGACY_UNVERIFIED'
    for change in ({'trace_id':None},{'trace_status':'DEGRADED'},{'trace_status':'LEGACY_UNVERIFIED'}):
        with pytest.raises(ValidationError):
            HumanDecision.model_validate_json(json.dumps(payload|change))
