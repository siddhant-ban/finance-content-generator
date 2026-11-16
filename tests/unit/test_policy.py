import json
from datetime import timedelta
from uuid import uuid4
import pytest
from app.contracts.workflow import ReviewManifest
from app.workflow.policy import PolicyEngine
from tests.integration.task4_fixtures import setup,artifact,manifest,NOW

def test_unknown_schema_and_client_passed_fail_closed(tmp_path):
    _,store,engine,_=setup(tmp_path)
    ref=store.put(b'{"kind":"synthetic_foundation","schema_id":"unknown","passed":true}','application/json','1.0')
    assert not engine.validate('candidate',ref).report.ready
    ref=artifact(store,'candidate',label='bad',passed=True)
    assert not engine.validate('candidate',ref).report.ready

def test_synthetic_validator_cannot_handle_editorial(tmp_path):
    _,store,engine,_=setup(tmp_path)
    ref=store.put(b'{"kind":"editorial","schema_id":"fixture.v1","label":"fixture"}','application/json','1.0')
    assert not engine.validate('candidate',ref).report.ready

def test_readiness_rechecks_evidence_rights_and_bytes(tmp_path):
    _,store,engine,_=setup(tmp_path); m=manifest(store,uuid4())
    assert engine.evaluate_readiness(m,NOW).ready
    for field,ref in [('verification',artifact(store,'verification')),('asset_register',artifact(store,'asset_register',licenses=[{'asset_id':'bad','rights':'unknown','expires_at':None}])),('asset_register',artifact(store,'asset_register',licenses=[{'asset_id':'expired','rights':'licensed','expires_at':(NOW-timedelta(seconds=1)).isoformat()}]))]:
        assert not engine.evaluate_readiness(m.model_copy(update={field:ref}),NOW).ready
    bad=m.model_copy(update={'claim_revision_refs':()})
    assert not engine.evaluate_readiness(bad,NOW).ready
    (store.root/m.script.sha256[:2]/m.script.sha256).write_bytes(b'corrupt')
    assert not engine.evaluate_readiness(m,NOW).ready

def test_readiness_checks_manifest_kind_and_current_claim_uses(tmp_path):
    _,store,engine,_=setup(tmp_path); m=manifest(store,uuid4())
    assert not engine.evaluate_readiness(m.model_copy(update={'kind':'editorial'}),NOW).ready
    bad=artifact(store,'captions',uses=[{'claim_id':str(uuid4()),'claim_revision':1,'text':'unsupported'}])
    assert not engine.evaluate_readiness(m.model_copy(update={'captions':bad}),NOW).ready

@pytest.mark.parametrize('stage',['render','attributions','asset_register'])
def test_readiness_rejects_explicit_unsupported_claim_uses_in_any_member(tmp_path,stage):
    _,store,engine,_=setup(tmp_path); m=manifest(store,uuid4())
    data=json.loads(store.read(getattr(m,stage)))
    data['uses']=[{'claim_id':str(uuid4()),'claim_revision':1,'text':'Unsupported additional fact'}]
    ref=store.put(json.dumps(data).encode(),'application/json','1.0')
    assert not engine.evaluate_readiness(m.model_copy(update={stage:ref}),NOW).ready


@pytest.mark.parametrize('stage',['render','script','captions','attributions'])
@pytest.mark.parametrize('rights,expires',[('unknown',None),('licensed','2026-11-09T00:00:00Z')])
def test_readiness_rechecks_explicit_rights_in_every_member(tmp_path,stage,rights,expires):
    _,store,engine,_=setup(tmp_path); m=manifest(store,uuid4())
    data=json.loads(store.read(getattr(m,stage)))
    data['licenses']=[{'asset_id':'member-asset','rights':rights,'expires_at':expires}]
    ref=store.put(json.dumps(data).encode(),'application/json','1.0')
    assert not engine.evaluate_readiness(m.model_copy(update={stage:ref}),NOW).ready
