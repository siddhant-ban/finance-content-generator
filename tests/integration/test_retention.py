"""Retention only removes old completed unreferenced fixture evidence."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import pytest
from app.evals.datasets import load_dataset
from app.evals.evaluators import foundation_evaluators
from app.evals.models import ExperimentConfig
from app.evals.run import create_runner, run_experiment, persist_bundle
from app.storage.db import open_database, migrate

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)

@pytest.fixture
def local(tmp_path):
    runner = create_runner(tmp_path/'evals/runtime')
    bundle = run_experiment(ExperimentConfig(), load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'), runner, foundation_evaluators())
    runner.telemetry.shutdown(0)
    runner.store.conn.close()
    conn = open_database(tmp_path/'state.db'); migrate(conn); conn.close()
    return tmp_path, bundle

def aged(bundle, root, age):
    end = NOW-age
    records = tuple(row.model_copy(update={'started_at':end-timedelta(seconds=1), 'finished_at':end}) for row in bundle.records)
    return persist_bundle(bundle.model_copy(update={'records':records}), root/'evals')

def snapshot(root):
    return {str(p.relative_to(root)):(p.stat().st_ino,p.stat().st_mtime_ns,p.read_bytes()) for p in root.rglob('*') if p.is_file()}

def counts(root):
    result = {}
    for path in root.rglob('*.db'):
        conn=sqlite3.connect(f'{path.as_uri()}?immutable=1',uri=True)
        result[str(path)]={name:conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    return result

def test_retention_30_days_preserves_active_refs_baselines_pending_outbox(local):
    from app.maintenance import cleanup
    root,bundle=local
    old=aged(bundle,root,timedelta(days=31)); boundary=aged(bundle,root,timedelta(days=30))
    pinned=aged(bundle,root,timedelta(days=32))
    from app.release.models import ReleaseCandidate, EvidencePin
    candidate=ReleaseCandidate(evidence={s:EvidencePin(path=str(pinned),bundle_hash=pinned.stem) for s in ('dev','holdout','prod_regression')})
    conn=sqlite3.connect(root/'state.db')
    conn.execute('INSERT INTO release_candidates VALUES (?,?,?)',(candidate.record_hash(),candidate.model_dump_json(),json.dumps({s:json.loads(pinned.read_text())['bundle'] for s in candidate.evidence})))
    conn.commit(); conn.close()
    before=counts(root)
    report=cleanup(NOW,False,data_dir=root)
    assert {r.ref for r in report.deleted} == {str(old.relative_to(root))}
    assert boundary.exists() and pinned.exists()
    assert counts(root)==before
    assert (root/'evals/runtime/telemetry.db').exists()

def test_dry_run_does_not_delete(local):
    from app.maintenance import cleanup
    root,bundle=local
    old=aged(bundle,root,timedelta(days=31))
    before=snapshot(root); rows=counts(root)
    report=cleanup(NOW,data_dir=root)
    assert [r.ref for r in report.eligible]==[str(old.relative_to(root))]
    assert not report.deleted and report.eligible_bytes==old.stat().st_size
    assert snapshot(root)==before and counts(root)==rows
    absent=root/'absent'
    assert not cleanup(NOW,data_dir=absent).eligible
    assert not absent.exists()

def test_retention_failure_keeps_audit_and_reports_error(local,monkeypatch):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31)); before=counts(root)
    original=Path.unlink
    def fail(path,*args,**kwargs):
        if path==old: raise PermissionError('private sensitive message')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',fail)
    report=cleanup(NOW,False,data_dir=root)
    assert old.exists() and not report.deleted and report.errors
    assert 'private sensitive message' not in report.model_dump_json()
    assert counts(root)==before

def test_malformed_reference_fails_closed(local):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    conn=sqlite3.connect(root/'state.db'); conn.execute('INSERT INTO release_candidates VALUES (?,?,?)',('a'*64,'{}','{}')); conn.commit(); conn.close()
    report=cleanup(NOW,False,data_dir=root)
    assert old.exists() and report.errors and not report.deleted

def test_incomplete_and_unpublished_evidence_protected(local):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    from app.evals.run import read_bundle
    from app.evals.evaluators import foundation_evaluators
    runner=create_runner(root/'incomplete-runtime')
    try:
        incomplete=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'),
            runner,foundation_evaluators()[:-1])
    finally:
        runner.telemetry.shutdown(0)
        runner.store.conn.close()
    canonical=aged(incomplete,root,timedelta(days=31))
    assert read_bundle(canonical).status=='INCOMPLETE'
    pending=root/'evals/.pending-x'
    pending.write_bytes(old.read_bytes())
    report=cleanup(NOW,data_dir=root)
    assert (str(canonical.relative_to(root)), 'completion_unproven') in {(r.ref,r.reason) for r in report.protected}
    assert any(r.ref==str(pending.relative_to(root)) for r in report.protected)


def test_empty_review_note_does_not_protect_cwd(local,monkeypatch):
    from app.maintenance import _referenced
    root,bundle=local
    monkeypatch.chdir(root)
    path=root/'evals'/'artifact.json'
    assert not _referenced(path,bundle,{''})
    assert _referenced(path,bundle,{'evals'})

def test_reference_recheck_prevents_new_pin_deletion(local, monkeypatch):
    import app.maintenance as maintenance
    from app.release.models import ReleaseCandidate, EvidencePin
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    candidate=ReleaseCandidate(evidence={s:EvidencePin(path=str(old),bundle_hash=old.stem) for s in ('dev','holdout','prod_regression')})
    read=maintenance._references
    calls=[]
    def add_pin(connections):
        result=read(connections)
        calls.append(1)
        if len(calls)==1:
            connections[0].execute('INSERT INTO release_candidates VALUES (?,?,?)',(candidate.record_hash(),candidate.model_dump_json(),json.dumps({s:json.loads(old.read_text())['bundle'] for s in candidate.evidence})))
        return result
    monkeypatch.setattr(maintenance,'_references',add_pin)
    report=maintenance.cleanup(NOW,False,data_dir=root)
    assert len(calls)>=2 and old.exists() and not report.deleted
    assert any(r.reason=='referenced_history' for r in report.protected)

def test_dry_run_nonempty_wal_is_fail_closed_without_mutation(local):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    conn=open_database(root/'state.db')
    conn.execute('CREATE TABLE wal_fixture(value TEXT)'); conn.commit()
    before=snapshot(root)
    report=cleanup(NOW,data_dir=root)
    assert old.exists() and not report.eligible
    assert 'reference_metadata_unavailable' in report.errors
    assert snapshot(root)==before
    conn.close()

def test_missing_business_reference_database_fails_closed(local):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    (root/'state.db').unlink(); (root/'evals/runtime/state.db').unlink()
    report=cleanup(NOW,False,data_dir=root)
    assert old.exists() and not report.deleted and report.errors

@pytest.mark.parametrize('dry_run', [True, False])
@pytest.mark.parametrize('damage', ['missing_table', 'missing_column'])
def test_incomplete_reference_schema_fails_closed(local, dry_run, damage):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    conn=sqlite3.connect(root/'state.db')
    if damage=='missing_table':
        conn.execute('DROP TABLE feedback_quarantine')
    else:
        conn.execute('ALTER TABLE execution_requests RENAME COLUMN input_envelope TO lost_input')
    conn.commit(); conn.close()
    report=cleanup(NOW,dry_run,data_dir=root)
    assert old.exists() and not report.eligible and not report.deleted
    assert 'reference_metadata_unavailable' in report.errors


def test_file_root_reports_safe_error_and_cli_json(tmp_path, monkeypatch, capsys):
    from app.maintenance import cleanup, main
    root=tmp_path/'file'; root.write_text('private text')
    report=cleanup(NOW,data_dir=root)
    assert report.errors == ('filesystem_unavailable',)
    monkeypatch.setenv('FINANCE_DATA_DIR',str(root))
    assert main(['--dry-run'])==2
    output=capsys.readouterr().out
    assert json.loads(output)['errors']==['filesystem_unavailable']
    assert 'private text' not in output


def test_unreadable_evals_directory_reports_safe_error(local, monkeypatch):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    original=Path.iterdir
    def unreadable(path):
        if path==root/'evals': raise PermissionError('private exception')
        return original(path)
    monkeypatch.setattr(Path,'iterdir',unreadable)
    report=cleanup(NOW,False,data_dir=root)
    assert old.exists() and not report.deleted
    assert report.errors==('filesystem_unavailable',)
    assert 'private exception' not in report.model_dump_json()


def test_unreadable_protected_entry_stat_protects_candidates(local, monkeypatch):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    private=root/'unreadable'; private.write_bytes(b'private')
    original=Path.lstat
    def unreadable(path,*args,**kwargs):
        if path==private: raise PermissionError('private exception')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'lstat',unreadable)
    report=cleanup(NOW,False,data_dir=root)
    assert old.exists() and not report.deleted
    assert 'filesystem_unavailable' in report.errors


def test_relative_path_only_quarantine_reference_survives_execute(local, monkeypatch):
    from app.maintenance import cleanup
    from app.release.models import FeedbackAnnotation, FeedbackCandidate
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    monkeypatch.chdir(root.parent)
    relative=str(old.relative_to(root.parent))
    annotation=FeedbackAnnotation(annotation_ref=relative, annotation_hash='a'*64,
        agent_name='foundation', dataset_hash='b'*64)
    feedback=FeedbackCandidate(feedback_id='c'*64, annotation=annotation, queued_at=NOW)
    conn=sqlite3.connect(root/'state.db')
    conn.execute('INSERT INTO feedback_quarantine VALUES (?,?)',(feedback.feedback_id,feedback.model_dump_json()))
    conn.commit(); conn.close()
    report=cleanup(NOW,False,data_dir=root)
    assert old.exists() and not report.deleted
    assert any(r.ref==str(old.relative_to(root)) and r.reason=='referenced_history' for r in report.protected)


@pytest.mark.parametrize('dry_run', [True, False])
@pytest.mark.parametrize('slot', ['state.db', 'evals/runtime'])
def test_dangling_authority_link_fails_closed(local, dry_run, slot):
    from app.maintenance import cleanup
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    path=root/slot
    if path.is_dir():
        path.rename(root/'saved-runtime')
    else:
        path.unlink()
    path.symlink_to(root/'missing-authority')
    report=cleanup(NOW,dry_run,data_dir=root)
    assert old.exists() and not report.eligible and not report.deleted
    assert 'reference_metadata_unavailable' in report.errors


def test_canonical_fifo_never_reaches_bundle_reader(local, monkeypatch):
    import os
    import app.maintenance as maintenance
    root,bundle=local
    fifo=root/'evals'/('0'*64+'.json'); os.mkfifo(fifo)
    original=maintenance.read_bundle
    called=[]
    def read(path):
        called.append(path)
        if path==fifo: raise ValueError('must not open FIFO')
        return original(path)
    monkeypatch.setattr(maintenance,'read_bundle',read)
    report=maintenance.cleanup(NOW,data_dir=root)
    assert fifo not in called
    assert not report.eligible and not report.deleted
    assert any(r.ref==str(fifo.relative_to(root)) and r.reason=='immutable_or_pending_or_unsupported' for r in report.protected)


def test_basename_only_quarantine_reference_survives_execute(local, monkeypatch):
    from app.maintenance import cleanup
    from app.release.models import FeedbackAnnotation, FeedbackCandidate
    root,bundle=local; old=aged(bundle,root,timedelta(days=31))
    monkeypatch.chdir(old.parent)
    annotation=FeedbackAnnotation(annotation_ref=old.name, annotation_hash='a'*64,
        agent_name='foundation', dataset_hash='b'*64)
    feedback=FeedbackCandidate(feedback_id='c'*64, annotation=annotation, queued_at=NOW)
    conn=sqlite3.connect(root/'state.db')
    conn.execute('INSERT INTO feedback_quarantine VALUES (?,?)',(feedback.feedback_id,feedback.model_dump_json()))
    conn.commit(); conn.close()
    report=cleanup(NOW,False,data_dir=root)
    assert old.exists() and not report.deleted
    assert any(r.ref==str(old.relative_to(root)) and r.reason=='referenced_history' for r in report.protected)
