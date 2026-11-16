"""Explicitly synthetic deterministic validators; no production policies."""
import json
from datetime import datetime, timezone
from uuid import uuid4
from app.contracts.common import ArtifactRef
from app.contracts.evidence import SourceRecord, ClaimRevision, EvidenceRef, VerificationRecord, ClaimUse
from app.contracts.workflow import ReviewManifest, PolicyReport, PolicyCheck, StoryState
from app.storage.db import open_database, migrate
from app.storage.artifacts import ArtifactStore
from app.workflow.policy import PolicyEngine, ArtifactValidation, AssetLicense
from app.workflow.state import StateService, TransitionRequest
NOW=datetime(2026,11,10,tzinfo=timezone.utc)
STAGES=('candidate','cluster','score','dossier','verification','script','render','subtitles','captions','charts','attributions','asset_register')
def validator(data):
    if data.get('label') != 'fixture':
        raise ValueError('fixture payload required')
    return ArtifactValidation(
        report=PolicyReport(ready=True, checks=(PolicyCheck(code='synthetic_fixture',passed=True),)),
        decision=data.get('decision'), eligible=data.get('eligible',False),
        claims=tuple(ClaimRevision.model_validate_json(json.dumps(x)) for x in data.get('claims',[])),
        sources=tuple(SourceRecord.model_validate_json(json.dumps(x)) for x in data.get('sources',[])),
        verdicts=tuple(VerificationRecord.model_validate_json(json.dumps(x)) for x in data.get('verdicts',[])),
        uses=tuple(ClaimUse.model_validate_json(json.dumps(x)) for x in data.get('uses',[])),
        licenses=tuple(AssetLicense.model_validate_json(json.dumps(x)) for x in data.get('licenses',[])))
def setup(tmp_path):
    conn=open_database(tmp_path/'state.db'); migrate(conn)
    store=ArtifactStore(tmp_path/'artifacts',conn)
    engine=PolicyEngine(store)
    for stage in STAGES:
        engine.register(stage,'fixture.v1','synthetic_foundation',validator)
    service=StateService(conn,store,engine,clock=lambda:NOW)
    return conn,store,engine,service

def artifact(store,stage,**fields):
    data={'schema_id':'fixture.v1','kind':'synthetic_foundation','label':'fixture','stage':stage,**fields}
    return store.put(json.dumps(data,sort_keys=True).encode(),'application/json','1.0')

def manifest(store,story_id):
    source=SourceRecord.model_validate_json(open('tests/fixtures/evidence/valid.json').read())
    ref=EvidenceRef(source_id=source.source_id,source_revision=1,source_record_hash=source.record_hash())
    claim=ClaimRevision(claim_id=uuid4(),revision=1,text='Synthetic fact',claim_type='fact',materiality='material',evidence_refs=(ref,))
    verdict=VerificationRecord(claim_id=claim.claim_id,claim_revision=1,claim_hash=claim.record_hash(),status='VERIFIED',evidence_refs=(ref,))
    use=ClaimUse(claim_id=claim.claim_id,claim_revision=1,text=claim.text)
    refs={stage:artifact(store,stage) for stage in STAGES[3:]}
    refs['dossier']=artifact(store,'dossier',sources=[source.model_dump(mode='json')],claims=[claim.model_dump(mode='json')])
    refs['verification']=artifact(store,'verification',verdicts=[verdict.model_dump(mode='json')])
    for stage in ('script','captions','subtitles','charts'):
        refs[stage]=artifact(store,stage,uses=[use.model_dump(mode='json')])
    refs['asset_register']=artifact(store,'asset_register',licenses=[{'asset_id':'synthetic','rights':'owned','expires_at':None}])
    from app.contracts.workflow import RevisionRef
    return ReviewManifest(story_id=story_id,revision=1,kind='synthetic_foundation',claim_revision_refs=(RevisionRef(record_id=claim.claim_id,revision=1,sha256=claim.record_hash()),),source_revision_refs=(RevisionRef(record_id=source.source_id,revision=1,sha256=source.record_hash()),),**refs)

def create(service,store):
    return service.create(uuid4(),uuid4(),artifact(store,'candidate'),'test','create')
def advance(service,store,snapshot,target,**fields):
    stage={'CLUSTERED':'cluster','SCORED':'score','RESEARCHED':'dossier','VERIFIED':'verification','SCRIPTED':'script','RENDERED':'render'}.get(target)
    ref=artifact(store,stage,**fields) if stage else None
    return service.transition(TransitionRequest(story_id=snapshot.story_id,target=StoryState(target),expected_version=snapshot.version,caller_scope='test',idempotency_key=str(uuid4()),artifact=ref,reason_code='fixture'))

def awaiting(service,store):
    snapshot=create(service,store)
    m=manifest(store,snapshot.story_id)
    for target in ('CLUSTERED','SCORED','RESEARCHED','VERIFIED','SCRIPTED','RENDERED'):
        stage={'CLUSTERED':'cluster','SCORED':'score','RESEARCHED':'dossier','VERIFIED':'verification','SCRIPTED':'script','RENDERED':'render'}[target]
        ref=getattr(m,stage) if stage in ('dossier','verification','script','render') else artifact(store,stage,decision='ADVANCE',eligible=True)
        snapshot=service.transition(TransitionRequest(story_id=snapshot.story_id,target=StoryState(target),expected_version=snapshot.version,caller_scope='test',idempotency_key=str(uuid4()),artifact=ref))
    snapshot=service.transition(TransitionRequest(story_id=snapshot.story_id,target=StoryState.AWAITING_APPROVAL,expected_version=snapshot.version,caller_scope='test',idempotency_key=str(uuid4()),manifest=m))
    return snapshot,m
