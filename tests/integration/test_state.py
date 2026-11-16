import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from app.contracts.workflow import StoryState,ChangeKind
from app.workflow.state import StateService,TransitionRequest,StateConflict,PolicyDenied,allowed_edge
from app.storage.db import open_database
from app.storage.artifacts import ArtifactStore
from app.workflow.policy import PolicyEngine
from tests.integration.task4_fixtures import setup,artifact,create,advance,awaiting,manifest,NOW,STAGES,validator
# Named editorial transitions from the workflow specification.
REWORK = {'REJECTED','NEEDS_RESEARCH','NEEDS_REVISION'}
ALLOWED_TARGETS = {
    'DISCOVERED': REWORK | {'CLUSTERED'},
    'CLUSTERED': REWORK | {'SCORED'},
    'SCORED': REWORK | {'RESEARCHED'},
    'RESEARCHED': REWORK | {'VERIFIED'},
    'VERIFIED': REWORK | {'SCRIPTED'},
    'SCRIPTED': REWORK | {'RENDERED'},
    'RENDERED': REWORK | {'AWAITING_APPROVAL'},
    'AWAITING_APPROVAL': REWORK | {'AWAITING_APPROVAL','APPROVED_FOR_PUBLISH'},
    'APPROVED_FOR_PUBLISH': REWORK | {'AWAITING_APPROVAL'},
    'NEEDS_RESEARCH': {'DISCOVERED','CLUSTERED','SCORED','REJECTED'},
    'NEEDS_REVISION': {'DISCOVERED','CLUSTERED','SCORED','RESEARCHED','VERIFIED','SCRIPTED','RENDERED','REJECTED'},
    'PUBLISHED': set(),
    'ANALYZED': set(),
    'REJECTED': set(),
}

@pytest.mark.parametrize('source',list(StoryState))
@pytest.mark.parametrize('target',list(StoryState))
def test_all_state_pairs_against_matrix(source,target):
    assert allowed_edge(source,target)==(target.value in ALLOWED_TARGETS[source.value])

def test_agent_cannot_approve_or_publish(tmp_path):
    _,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    for target in ('APPROVED_FOR_PUBLISH','PUBLISHED','ANALYZED'):
        with pytest.raises(PolicyDenied): advance(svc,store,snap,target)

def test_actor_string_not_identity(tmp_path):
    _,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TransitionRequest(story_id=snap.story_id,target=StoryState.APPROVED_FOR_PUBLISH,expected_version=snap.version,caller_scope='human',idempotency_key='forged',actor_type='human')

def test_create_retry_same_request_one_event_changed_payload_conflicts(tmp_path):
    conn,store,_,svc=setup(tmp_path); sid,rid=uuid4(),uuid4(); candidate=artifact(store,'candidate')
    first=svc.create(sid,rid,candidate,'test','create')
    advance(svc,store,first,'CLUSTERED')
    assert svc.create(sid,rid,candidate,'test','create')==first
    assert len(svc.events(sid))==2
    with pytest.raises(StateConflict): svc.create(sid,uuid4(),candidate,'test','create')
    with pytest.raises(StateConflict): svc.create(sid,rid,candidate,'test','different')
    with pytest.raises(StateConflict): svc.create(uuid4(),rid,candidate,'test','badversion',1)

def test_idempotency_replay_and_payload_conflict(tmp_path):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    req=TransitionRequest(story_id=snap.story_id,target=StoryState.CLUSTERED,expected_version=1,caller_scope='test',idempotency_key='same',artifact=artifact(store,'cluster'))
    first=svc.transition(req); advance(svc,store,first,'SCORED')
    assert svc.transition(req)==first
    with pytest.raises(StateConflict): svc.transition(req.model_copy(update={'reason_code':'changed'}))

def test_restart_and_event_state_rollback(tmp_path):
    conn,store,engine,svc=setup(tmp_path); snap=create(svc,store)
    conn.execute("CREATE TRIGGER fail_event BEFORE INSERT ON state_events WHEN NEW.version=2 BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): advance(svc,store,snap,'CLUSTERED')
    assert svc.get(snap.story_id)==snap and len(svc.events(snap.story_id))==1
    conn.close(); conn=open_database(tmp_path/'state.db')
    new_store=ArtifactStore(tmp_path/'artifacts',conn)
    restarted=StateService(conn,new_store,PolicyEngine(new_store),clock=lambda:NOW)
    assert restarted.get(snap.story_id)==snap

def test_two_connections_one_version_wins(tmp_path):
    conn,store,_,svc=setup(tmp_path); snap=create(svc,store); ref=artifact(store,'cluster'); conn.close()
    def work(key):
        c=open_database(tmp_path/'state.db'); s=ArtifactStore(tmp_path/'artifacts',c); p=PolicyEngine(s)
        for stage in ('candidate','cluster'): p.register(stage,'fixture.v1','synthetic_foundation',validator)
        service=StateService(c,s,p,clock=lambda:NOW)
        try:
            service.transition(TransitionRequest(story_id=snap.story_id,target=StoryState.CLUSTERED,expected_version=1,caller_scope='test',idempotency_key=key,artifact=ref)); return True
        except StateConflict: return False
        finally: c.close()
    with ThreadPoolExecutor(max_workers=2) as pool: assert sorted(pool.map(work,['a','b']))==[False,True]

def test_direct_sql_forbidden_edges_and_audit_mutation_denied(tmp_path):
    conn,store,_,svc=setup(tmp_path); snap=create(svc,store)
    statements=[("UPDATE stories SET state='PUBLISHED',version=version+1 WHERE story_id=?",(str(snap.story_id),)),("UPDATE state_events SET reason_code='forged'",()),("DELETE FROM state_events",()),("INSERT OR REPLACE INTO state_events SELECT * FROM state_events",()),("INSERT OR REPLACE INTO stories SELECT * FROM stories",())]
    for sql,args in statements:
        with pytest.raises(sqlite3.IntegrityError): conn.execute(sql,args)
        conn.rollback()
    assert svc.get(snap.story_id)==snap

@pytest.mark.parametrize('origin',['DISCOVERED','CLUSTERED'])
def test_early_evidence_gap_corrects_and_resumes(tmp_path,origin):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    if origin=='CLUSTERED': snap=advance(svc,store,snap,origin)
    gap=advance(svc,store,snap,'NEEDS_RESEARCH')
    assert gap.resume_target.value==origin
    stage='candidate' if origin=='DISCOVERED' else 'cluster'
    resumed=svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState(origin),expected_version=gap.version,caller_scope='test',idempotency_key='correct',artifact=artifact(store,stage,correction=1)))
    assert resumed.state.value==origin
    if origin=='DISCOVERED': resumed=advance(svc,store,resumed,'CLUSTERED')
    resumed=advance(svc,store,resumed,'SCORED',decision='HOLD',eligible=True)
    with pytest.raises(PolicyDenied): advance(svc,store,resumed,'RESEARCHED')

def test_revision_only_saved_checkpoint(tmp_path):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store); snap=advance(svc,store,snap,'CLUSTERED'); gap=advance(svc,store,snap,'NEEDS_REVISION')
    with pytest.raises(PolicyDenied): advance(svc,store,gap,'SCRIPTED')
    assert advance(svc,store,gap,'CLUSTERED').state==StoryState.CLUSTERED

def test_research_resume_does_not_bypass_advance(tmp_path):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    for target in ('CLUSTERED','SCORED'): snap=advance(svc,store,snap,target,decision='HOLD',eligible=True)
    gap=advance(svc,store,snap,'NEEDS_RESEARCH'); resumed=advance(svc,store,gap,'SCORED',decision='HOLD',eligible=True)
    with pytest.raises(PolicyDenied): advance(svc,store,resumed,'RESEARCHED')

def test_hold_no_dispatch(tmp_path):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    snap=advance(svc,store,snap,'CLUSTERED'); snap=advance(svc,store,snap,'SCORED',decision='HOLD',eligible=True)
    with pytest.raises(PolicyDenied): advance(svc,store,snap,'RESEARCHED')
    assert svc.get(snap.story_id)==snap

@pytest.mark.parametrize('field,target',[('captions','RESEARCHED'),('script','RESEARCHED'),('subtitles','RESEARCHED'),('charts','RESEARCHED'),('dossier','SCORED'),('render','SCRIPTED')])
def test_forged_cosmetic_change_kind_cannot_keep_verification(tmp_path,field,target):
    _,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    changed=m.model_copy(update={field:artifact(store,field,changed=True),'revision':2})
    revised=svc.revise(snap.story_id,changed,ChangeKind.REVIEW_METADATA,snap.version,'edit')
    assert revised.state==StoryState.NEEDS_REVISION and revised.resume_target.value==target
    assert 'verification' not in revised.artifacts if target!='SCRIPTED' else 'verification' in revised.artifacts

def test_edit_revokes_approval_atomically(tmp_path):
    conn,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    # Private database fixture models a future authenticated Task12 transaction.
    conn.execute("INSERT INTO approval_decisions(decision_id,story_id,manifest_hash,story_version,principal_id) VALUES ('fixture',?,?,?,'private-fixture')",(str(snap.story_id),snap.manifest_hash,snap.version))
    conn.execute("UPDATE stories SET state='APPROVED_FOR_PUBLISH',version=version+1,approval_id='fixture' WHERE story_id=?",(str(snap.story_id),)); conn.commit()
    approved=svc.get(snap.story_id)
    revised=svc.revise(snap.story_id,m.model_copy(update={'revision':2,'internal_label':'corrected review copy'}),ChangeKind.REVIEW_METADATA,approved.version,'copy')
    assert revised.state==StoryState.AWAITING_APPROVAL and revised.approval_id is None
    assert svc.events(snap.story_id)[-1].manifest_hash==revised.manifest_hash

def test_supplied_unsupported_material_claims_block_verified_handoff(tmp_path):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    for stage in ('CLUSTERED','SCORED'): snap=advance(svc,store,snap,stage,decision='ADVANCE',eligible=True)
    bad_claim={'schema_version':'1.0','claim_id':str(uuid4()),'revision':1,'text':'Unsupported material claim','claim_type':'fact','materiality':'material','evidence_refs':[]}
    snap=advance(svc,store,snap,'RESEARCHED',claims=[bad_claim])
    # A supplied invalid claim cannot acquire approval through a fixture label.
    with pytest.raises(PolicyDenied): advance(svc,store,snap,'VERIFIED')
    assert svc.get(snap.story_id)==snap


def test_supplied_unsupported_script_uses_block_handoff(tmp_path):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store); m=manifest(store,snap.story_id)
    for target,stage in [('CLUSTERED','cluster'),('SCORED','score'),('RESEARCHED','dossier'),('VERIFIED','verification')]:
        ref=getattr(m,stage) if stage in ('dossier','verification') else artifact(store,stage,decision='ADVANCE',eligible=True)
        snap=svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState(target),expected_version=snap.version,caller_scope='test',idempotency_key=str(uuid4()),artifact=ref))
    bad_use={'claim_id':str(uuid4()),'claim_revision':1,'text':'Unsupported script fact'}
    with pytest.raises(PolicyDenied): advance(svc,store,snap,'SCRIPTED',uses=[bad_use])
    assert svc.get(snap.story_id)==snap


def test_manifest_cannot_substitute_valid_but_noncurrent_members(tmp_path):
    _,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    gap=advance(svc,store,snap,'NEEDS_REVISION')
    rendered=advance(svc,store,gap,'RENDERED')
    other=manifest(store,snap.story_id)
    with pytest.raises(PolicyDenied):
        svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState.AWAITING_APPROVAL,expected_version=rendered.version,caller_scope='test',idempotency_key='substitution',manifest=other))


def test_revision_rollback_preserves_approval_and_idempotent_original_response(tmp_path):
    conn,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    conn.execute("INSERT INTO approval_decisions VALUES ('rollback',?,?,?,'private-fixture')",(str(snap.story_id),snap.manifest_hash,snap.version))
    conn.execute("UPDATE stories SET state='APPROVED_FOR_PUBLISH',version=version+1,approval_id='rollback' WHERE story_id=?",(str(snap.story_id),)); conn.commit()
    old=svc.get(snap.story_id); count=len(svc.events(snap.story_id))
    revised=m.model_copy(update={'revision':2,'internal_label':'review'})
    conn.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON state_events WHEN NEW.reason_code='manifest_revised' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError): svc.revise(snap.story_id,revised,ChangeKind.REVIEW_METADATA,old.version,'rollback')
    assert svc.get(snap.story_id)==old and len(svc.events(snap.story_id))==count
    conn.execute('DROP TRIGGER fail_revision')
    first=svc.revise(snap.story_id,revised,ChangeKind.REVIEW_METADATA,old.version,'rollback')
    second=svc.revise(snap.story_id,revised.model_copy(update={'revision':3,'internal_label':'again'}),ChangeKind.REVIEW_METADATA,first.version,'new')
    assert svc.revise(snap.story_id,revised,ChangeKind.REVIEW_METADATA,old.version,'rollback')==first
    with pytest.raises(StateConflict): svc.revise(snap.story_id,revised.model_copy(update={'internal_label':'changed'}),ChangeKind.REVIEW_METADATA,old.version,'rollback')
    assert svc.get(snap.story_id)==second


def test_sql_approval_requires_exact_current_manifest_and_cannot_publish(tmp_path):
    conn,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    for approval in (None,'human'):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE stories SET state='APPROVED_FOR_PUBLISH',version=version+1,approval_id=? WHERE story_id=?",(approval,str(snap.story_id)))
        conn.rollback()
    conn.execute("INSERT INTO approval_decisions VALUES ('stale',?,?,?,'fixture')",(str(snap.story_id),snap.manifest_hash,snap.version-1)); conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE stories SET state='APPROVED_FOR_PUBLISH',version=version+1,approval_id='stale' WHERE story_id=?",(str(snap.story_id),))
    conn.rollback()
    for table in ('state_receipts','approval_decisions'):
        with pytest.raises(sqlite3.IntegrityError): conn.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}')
        conn.rollback()


def test_manifest_replay_returns_receipt_even_when_registered_manifest_file_missing(tmp_path):
    _,store,_,svc=setup(tmp_path); snap,m=awaiting(svc,store)
    new=m.model_copy(update={'revision':2,'internal_label':'review'})
    first=svc.revise(snap.story_id,new,ChangeKind.REVIEW_METADATA,snap.version,'receipt')
    (store.root/first.manifest_hash[:2]/first.manifest_hash).unlink()
    assert svc.revise(snap.story_id,new,ChangeKind.REVIEW_METADATA,snap.version,'receipt')==first


def test_render_cannot_accept_explicit_unknown_or_expired_rights(tmp_path):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    for target in ('CLUSTERED','SCORED','RESEARCHED','VERIFIED','SCRIPTED'):
        snap=advance(svc,store,snap,target,decision='ADVANCE',eligible=True)
    for rights,expires in [('unknown',None),('licensed','2026-01-01T00:00:00Z')]:
        with pytest.raises(PolicyDenied):
            advance(svc,store,snap,'RENDERED',licenses=[{'asset_id':'invalid','rights':rights,'expires_at':expires}])
    assert svc.get(snap.story_id)==snap


def test_factual_caption_reverifies(tmp_path):
    _,store,_,svc=setup(tmp_path); old,m=awaiting(svc,store)
    new=m.model_copy(update={'revision':2,'captions':artifact(store,'captions',uses=[{'claim_id':str(uuid4()),'claim_revision':1,'text':'New factual caption'}])})
    gap=svc.revise(old.story_id,new,ChangeKind.VISUAL,old.version,'caption')
    assert gap.resume_target==StoryState.RESEARCHED
    assert set(gap.artifacts)=={'candidate','cluster','score','dossier'}
    with pytest.raises(PolicyDenied): advance(svc,store,gap,'SCRIPTED')
    resumed=advance(svc,store,gap,'RESEARCHED')
    with pytest.raises(PolicyDenied): advance(svc,store,resumed,'SCRIPTED')


def test_workflow_contracts_reject_unknown_schema_version(tmp_path):
    from pydantic import ValidationError
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    data=snap.model_dump(); data['schema_version']='2.0'
    with pytest.raises(ValidationError): type(snap).model_validate(data)
    assert snap.schema_version=='1.0'


def test_synthetic_score_cannot_dispatch_editorial_dossier(tmp_path):
    conn,store,policy,svc=setup(tmp_path); snap=create(svc,store)
    for target in ('CLUSTERED','SCORED'): snap=advance(svc,store,snap,target,decision='ADVANCE',eligible=True)
    # A separately configured editorial policy cannot consume a synthetic decision.
    policy.register('dossier','editorial.test','editorial',validator)
    ref=store.put(b'{"kind":"editorial","schema_id":"editorial.test","label":"fixture"}','application/json','1.0')
    with pytest.raises(PolicyDenied):
        svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState.RESEARCHED,expected_version=snap.version,caller_scope='test',idempotency_key='mixed',artifact=ref))
    assert svc.get(snap.story_id)==snap


def test_research_correction_cannot_switch_synthetic_kind(tmp_path):
    _,store,policy,svc=setup(tmp_path); snap=create(svc,store); gap=advance(svc,store,snap,'NEEDS_RESEARCH')
    policy.register('candidate','editorial.test','editorial',validator)
    ref=store.put(b'{"kind":"editorial","schema_id":"editorial.test","label":"fixture"}','application/json','1.0')
    with pytest.raises(PolicyDenied):
        svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState.DISCOVERED,expected_version=gap.version,caller_scope='test',idempotency_key='switch',artifact=ref))


@pytest.mark.parametrize('with_evidence',[False,True])
def test_render_handoff_rejects_explicit_unsupported_uses(tmp_path,with_evidence):
    _,store,_,svc=setup(tmp_path); snap=create(svc,store)
    m=manifest(store,snap.story_id) if with_evidence else None
    for target,stage in [('CLUSTERED','cluster'),('SCORED','score'),('RESEARCHED','dossier'),('VERIFIED','verification'),('SCRIPTED','script')]:
        ref=getattr(m,stage) if m and stage in ('dossier','verification','script') else artifact(store,stage,decision='ADVANCE',eligible=True)
        snap=svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState(target),expected_version=snap.version,caller_scope='test',idempotency_key=str(uuid4()),artifact=ref))
    before_events=svc.events(snap.story_id)
    with pytest.raises(PolicyDenied):
        advance(svc,store,snap,'RENDERED',uses=[{'claim_id':str(uuid4()),'claim_revision':1,'text':'Unsupported render fact'}])
    assert svc.get(snap.story_id)==snap
    assert svc.events(snap.story_id)==before_events


def test_expired_render_rights_block_later_awaiting_handoff(tmp_path):
    from datetime import timedelta
    _,store,policy,svc=setup(tmp_path); snap=create(svc,store)
    m=manifest(store,snap.story_id)
    render=artifact(store,'render',licenses=[{'asset_id':'render','rights':'licensed','expires_at':(NOW+timedelta(seconds=1)).isoformat()}])
    m=m.model_copy(update={'render':render})
    for target,stage in [('CLUSTERED','cluster'),('SCORED','score'),('RESEARCHED','dossier'),('VERIFIED','verification'),('SCRIPTED','script'),('RENDERED','render')]:
        ref=getattr(m,stage) if stage in ('dossier','verification','script','render') else artifact(store,stage,decision='ADVANCE',eligible=True)
        snap=svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState(target),expected_version=snap.version,caller_scope='test',idempotency_key=str(uuid4()),artifact=ref))
    assert policy.evaluate_readiness(m,NOW).ready
    svc.clock=lambda:NOW+timedelta(seconds=2)
    before_events=svc.events(snap.story_id)
    with pytest.raises(PolicyDenied):
        svc.transition(TransitionRequest(story_id=snap.story_id,target=StoryState.AWAITING_APPROVAL,expected_version=snap.version,caller_scope='test',idempotency_key='expired-render',manifest=m))
    assert svc.get(snap.story_id)==snap
    assert svc.events(snap.story_id)==before_events
