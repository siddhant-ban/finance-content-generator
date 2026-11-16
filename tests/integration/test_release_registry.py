"""Authenticated promotion is atomic, versioned and replay safe."""
import sqlite3
import pytest
from tests.release_fixtures import evidence,candidate_for,HUMAN,AGENT,trusted
from app.release.service import ReleaseService
from app.storage.db import migrate,open_database

@pytest.fixture
def registry(tmp_path):
    conn=open_database(tmp_path/'registry.db'); migrate(conn)
    service=ReleaseService(conn,current_session_verifier=trusted)
    yield service
    conn.close()

def test_eligibility_not_approval(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    assert registry.active('foundation') is None
    for principal in (AGENT,HUMAN):
        denied=ReleaseService(registry.conn)
        with pytest.raises(ValueError):
            denied.approve(candidate.record_hash(),principal,0,True,'key')
    with pytest.raises(ValueError):
        registry.approve(candidate.record_hash(),AGENT,0,True,'key')
    with pytest.raises(ValueError):
        registry.approve(candidate.record_hash(),HUMAN,0,False,'key')

def test_release_lost_response_retry_returns_original_one_event(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    receipt=registry.approve(candidate.record_hash(),HUMAN,0,True,'key')
    assert registry.approve(candidate.record_hash(),HUMAN,0,True,'key')==receipt
    assert registry.active('foundation')==receipt
    assert registry.conn.execute('SELECT count(*) FROM release_events').fetchone()[0]==1
    with pytest.raises(sqlite3.IntegrityError):
        registry.conn.execute("UPDATE release_approvals SET receipt='{}'")

def test_release_key_changed_payload_conflicts(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'key')
    with pytest.raises(ValueError):
        registry.approve(candidate.record_hash(),HUMAN,1,True,'key')

def test_revoked_release_retry_does_not_reactivate(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    receipt=registry.approve(candidate.record_hash(),HUMAN,0,True,'key')
    registry.revoke('foundation',HUMAN,1)
    assert registry.active('foundation') is None
    assert registry.approve(candidate.record_hash(),HUMAN,0,True,'key')==receipt
    assert registry.active('foundation') is None
    assert registry.check(candidate).status=='ELIGIBLE'
    with pytest.raises(ValueError):
        registry.approve(candidate.record_hash(),HUMAN,2,True,'bootstrap-again')

def test_incomparable_baseline_blocks(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'key')
    changed={split:bundle.model_copy(update={'config':bundle.config.model_copy(update={'seed':99}),
        'provenance':{**bundle.provenance,'experiment_config_hash':bundle.config.model_copy(update={'seed':99}).record_hash()}})
        for split,bundle in evidence.items()}
    assert registry.check(candidate_for(changed,tmp_path/'other')).status=='BLOCKED'

def test_session_verified_before_replay(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'key')
    for verifier in (lambda p:False,lambda p:'true'):
        denied=ReleaseService(registry.conn,current_session_verifier=verifier)
        with pytest.raises(ValueError):
            denied.approve(candidate.record_hash(),HUMAN,0,True,'key')


def test_exact_approved_guard_remains_synthetic(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    receipt=registry.approve(candidate.record_hash(),HUMAN,0,True,'key')
    assert registry.require_approved('foundation',receipt.release_pins,validators_ready=True,
        execution_kind='foundation_demo')==receipt
    for changes in ({'config_hash':'a'*64},{'model_hash':'a'*64},{'prompt_hash':'a'*64}):
        with pytest.raises(ValueError):
            registry.require_approved('foundation',{**receipt.release_pins,**changes},validators_ready=True,execution_kind='foundation_demo')
    with pytest.raises(ValueError):
        registry.require_approved('foundation',receipt.release_pins,validators_ready=True)
    with pytest.raises(ValueError):
        registry.require_approved('foundation',receipt.release_pins,validators_ready=False,execution_kind='foundation_demo')


def test_changed_candidate_after_revocation_cannot_bootstrap(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'first')
    registry.revoke('foundation',HUMAN,1)
    other=candidate_for(evidence,tmp_path/'other')
    assert other.record_hash()!=candidate.record_hash()
    assert registry.check(other).status=='ELIGIBLE'
    with pytest.raises(ValueError):
        registry.approve(other.record_hash(),HUMAN,2,True,'second')
    next_receipt=registry.approve(other.record_hash(),HUMAN,2,False,'second')
    assert next_receipt.baseline_hash==candidate.record_hash()


def test_approved_baseline_cli_uses_immutable_registry(registry,evidence,tmp_path):
    import subprocess,sys,json
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'first')
    registry_path=registry.conn.execute('PRAGMA database_list').fetchone()[2]
    command=[sys.executable,'-m','app.evals.compare','--agent','foundation','--candidate',
        candidate.evidence['holdout'].path,'--baseline','approved','--registry',registry_path]
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['status']=='PASS'
    manifest=tmp_path/'candidate.json'; manifest.write_text(candidate.model_dump_json())
    gate=[sys.executable,'-m','app.release.check','--agent','foundation','--candidate',str(manifest),'--registry',registry_path]
    result=subprocess.run(gate,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['status']=='APPROVED'
    registry.revoke('foundation',HUMAN,1)
    assert subprocess.run(gate,capture_output=True).returncode==1
    manifest.write_text('{}')
    assert subprocess.run(gate,capture_output=True).returncode==2


def test_baseline_survives_source_bundle_removal(registry,evidence,tmp_path):
    from pathlib import Path
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    receipt=registry.approve(candidate.record_hash(),HUMAN,0,True,'first')
    for split,pin in candidate.evidence.items():
        Path(pin.path).unlink()
        assert registry.approved_baseline('foundation',split).record_hash()==receipt.experiment_hashes[split]
    other=candidate_for(evidence,tmp_path/'other')
    assert registry.check(other).status=='ELIGIBLE'


def test_gate_cli_without_registry_requires_bootstrap(evidence,tmp_path):
    import subprocess,sys,json
    candidate=candidate_for(evidence,tmp_path/'bundles')
    manifest=tmp_path/'candidate.json'; manifest.write_text(candidate.model_dump_json())
    result=subprocess.run([sys.executable,'-m','app.release.check','--agent','foundation',
        '--candidate',str(manifest),'--registry',str(tmp_path/'new'/'registry.db')],capture_output=True,text=True)
    assert result.returncode==1,result.stdout+result.stderr
    assert json.loads(result.stdout)['status']=='REQUIRES_BOOTSTRAP_APPROVAL'


def test_session_expired_while_acquiring_lock_denies_replay(registry,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'first')
    checks=iter((True,False))
    expiring=ReleaseService(registry.conn,current_session_verifier=lambda principal:next(checks))
    with pytest.raises(ValueError):
        expiring.approve(candidate.record_hash(),HUMAN,0,True,'first')


@pytest.mark.parametrize('identity',['candidate','approval_id','approval_version','receipt','event','feedback'])
def test_replace_cannot_rewrite_release_history(registry,evidence,tmp_path,identity):
    from app.release.models import FeedbackAnnotation
    from app.evals.run import digest
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'first')
    note=tmp_path/'note.json'; note.write_text('{}')
    registry.queue_feedback(FeedbackAnnotation(annotation_ref=str(note),annotation_hash=digest(note.read_bytes()),agent_name='foundation',dataset_hash='a'*64))
    assert registry.conn.execute('PRAGMA recursive_triggers').fetchone()[0]==0
    table={'candidate':'release_candidates','approval_id':'release_approvals','approval_version':'release_approvals',
        'receipt':'release_receipts','event':'release_events','feedback':'feedback_quarantine'}[identity]
    original=registry.conn.execute('SELECT * FROM '+table).fetchall()
    values=list(original[0])
    if identity=='approval_version':
        values[0]='replacement-id'
    elif identity=='receipt':
        values[2]='replacement-fingerprint'
    else:
        values[-1]='replacement-content'
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        registry.conn.execute('INSERT OR REPLACE INTO '+table+' VALUES ('+','.join('?' for _ in values)+')',values)
    registry.conn.rollback()
    assert registry.conn.execute('SELECT * FROM '+table).fetchall()==original
    # Intentional same-candidate and quarantine retries are service-level lookups.
    assert registry.check(candidate).status=='APPROVED'
    first=registry.queue_feedback(FeedbackAnnotation(annotation_ref=str(note),annotation_hash=digest(note.read_bytes()),agent_name='foundation',dataset_hash='a'*64))
    assert registry.queue_feedback(first.annotation)==first


def test_approved_comparison_does_not_create_registry(tmp_path):
    import subprocess,sys
    root=tmp_path/'missing.db'
    result=subprocess.run([sys.executable,'-m','app.evals.compare','--agent','foundation',
        '--candidate','tests/fixtures/evals/synthetic-baseline-v1.1.json','--baseline','approved','--registry',str(root)],capture_output=True)
    assert result.returncode==2
    assert not root.exists()


def test_approved_comparison_uses_existing_readonly_registry(registry,evidence,tmp_path):
    import subprocess,sys,json
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'first')
    path=tmp_path/'registry.db'
    registry.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    # Lookup must not require the migration ledger or any write access.
    registry.conn.execute('DROP TABLE schema_migrations'); registry.conn.commit()
    path.chmod(0o400)
    try:
        result=subprocess.run([sys.executable,'-m','app.evals.compare','--agent','foundation','--candidate',
            candidate.evidence['dev'].path,'--baseline','approved','--registry',str(path)],capture_output=True,text=True)
        assert result.returncode==0,result.stdout+result.stderr
        assert json.loads(result.stdout)['status']=='PASS'
        assert not registry.conn.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone()
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize('alias',['rowid','oid','_rowid_'])
@pytest.mark.parametrize('table',['release_candidates','release_approvals','release_receipts','release_events','feedback_quarantine'])
def test_hidden_rowid_alias_cannot_replace_release_history(registry,evidence,tmp_path,table,alias):
    from app.release.models import FeedbackAnnotation
    from app.evals.run import digest
    candidate=candidate_for(evidence,tmp_path/'bundles'); registry.check(candidate)
    registry.approve(candidate.record_hash(),HUMAN,0,True,'first')
    note=tmp_path/'note.json'; note.write_text('{}')
    registry.queue_feedback(FeedbackAnnotation(annotation_ref=str(note),annotation_hash=digest(note.read_bytes()),agent_name='foundation',dataset_hash='a'*64))
    assert registry.conn.execute('PRAGMA recursive_triggers').fetchone()[0]==0
    original=registry.conn.execute('SELECT * FROM '+table).fetchall()
    values=list(original[0])
    # A distinct logical identity avoids every logical-key reinsert guard.
    if table=='release_approvals':
        values[0]='new-approval-id'; values[3]=99
    elif table=='release_receipts':
        values[0]='new-scope'; values[1]='new-key'
    else:
        values[0]='new-logical-id'
    columns=[row[1] for row in registry.conn.execute('PRAGMA table_info('+table+')')]
    statement='INSERT OR REPLACE INTO '+table+' ('+alias+','+','.join(columns)+') VALUES ('+','.join('?' for _ in range(len(values)+1))+')'
    with pytest.raises(sqlite3.OperationalError,match='no column named'):
        registry.conn.execute(statement,(1,*values))
    registry.conn.rollback()
    assert registry.conn.execute('SELECT * FROM '+table).fetchall()==original
