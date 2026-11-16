"""Phoenix boundary: real harness evidence, external SDK replaced only at I/O."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.evals.datasets import load_dataset
from app.evals.evaluators import foundation_evaluators
from app.evals.models import ExperimentConfig
from app.evals.run import create_runner, run_experiment, read_bundle
from app.evals.phoenix import PhoenixAdapter, load_offline, safe_client
from app.observability.tracing import Telemetry, readable


class SDK:
    def __init__(self, failure=None):
        self.datasets = self.experiments = self
        self.uploads, self.exps, self.runs, self.evals = [], [], [], []
        self.failure = failure

    def create_dataset(self, *, name, examples, timeout):
        if self.failure == 'dataset':
            raise RuntimeError('SECRET remote response')
        self.uploads.append(examples)
        return SimpleNamespace(id='RGF0YXNldDox', version_id='VmVyc2lvbjox', examples=[
            {'id':f'ZXhhbXBsZTo{i}', 'input':e['input'], 'output':e['output'], 'metadata':e['metadata']}
            for i,e in enumerate(examples)])

    def create(self, *, dataset_id, dataset_version_id, experiment_name, experiment_metadata, repetitions, timeout):
        self.exps.append(locals().copy())
        return {'id':'RXhwZXJpbWVudDox'}

    def log_run(self, *, experiment_id, dataset_example_id, output, start_time, end_time,
                repetition_number, trace_id, error, timeout):
        self.runs.append({k:v for k,v in locals().items() if k != 'self'})
        return {'id':f'cnVu{len(self.runs)}'}

    def log_evaluation(self, *, experiment_run_id, name, annotator_kind, score, label, explanation,
                       error, metadata, trace_id, start_time, end_time, timeout):
        if self.failure == 'evaluation':
            raise RuntimeError('SECRET private body')
        self.evals.append({k:v for k,v in locals().items() if k != 'self'})
        return {'id':f'ZXZhb{len(self.evals)}'}


@pytest.fixture
def evidence(tmp_path):
    def make(split='dev', repeats=1):
        ds = load_dataset(Path('evals/foundation/1.0.0'), split, 'gate')
        runner = create_runner(tmp_path / f'{split}-{repeats}')
        try:
            return ds, run_experiment(ExperimentConfig(repeats=repeats), ds, runner, foundation_evaluators())
        finally:
            runner.telemetry.shutdown(0)
            runner.store.conn.close()
    return make


def adapter(tmp_path, sdk):
    return PhoenixAdapter(tmp_path/'registry.db', client=sdk)


def test_register_all_examples_annotations_and_scores(tmp_path, evidence):
    ds, bundle = evidence(repeats=2)
    sdk = SDK(); a = adapter(tmp_path,sdk)
    reg = a.register_dataset(ds)
    assert reg.status == 'registered' and reg.dataset_hash == ds.dataset_hash
    published = a.publish_experiment(bundle)
    assert published.status == 'registered'
    assert len(sdk.uploads[0]) == 3 and len(sdk.runs) == 6 and len(sdk.evals) == 36
    for row, run in zip(bundle.records,sdk.runs):
        assert run['dataset_example_id'] == f'ZXhhbXBsZTo{int(row.example_id[-1])-1}'
        assert run['start_time'] == row.started_at and run['end_time'] == row.finished_at
        assert run['repetition_number'] == row.repeat+1
        assert run['trace_id'] == row.output.trace_id
    assert {e['experiment_run_id'] for e in sdk.evals} == {f'cnVu{i}' for i in range(1,7)}
    assert all(e['score'] == 1 and e['label'] == 'PASS' for e in sdk.evals)
    assert sdk.exps[0]['dataset_version_id'] == 'VmVyc2lvbjox'


def test_idempotent_version_hash_registration(tmp_path, evidence):
    ds,bundle = evidence(); sdk=SDK()
    first = adapter(tmp_path,sdk).publish_experiment(bundle)
    second = adapter(tmp_path,sdk).publish_experiment(bundle)
    assert first == second and len(sdk.uploads) == len(sdk.exps) == 1
    assert len(sdk.runs) == 3 and len(sdk.evals) == 18
    other,_ = evidence('prod_regression')
    adapter(tmp_path,sdk).register_dataset(other)
    assert len(sdk.uploads) == 2


def test_phoenix_failure_preserves_complete_local_bundle(tmp_path, evidence):
    _,bundle = evidence(); a=adapter(tmp_path,SDK('dataset'))
    path = a.export_offline(bundle,tmp_path/'offline')
    registration = a.publish_experiment(bundle)
    assert registration.status == 'failed' and registration.error_code == 'remote_unavailable'
    loaded = load_offline(path)
    assert loaded.bundle.record_hash() == bundle.record_hash()
    assert len(loaded.payload['runs']) == 3
    assert len(loaded.payload['runs'][0]['evaluations']) == 6
    assert loaded.payload['runs'][0]['trace_id'] == bundle.records[0].output.trace_id
    assert loaded.payload['runs'][0]['span_id'] == bundle.records[0].output.span_id
    assert 'SECRET' not in registration.model_dump_json()
    assert read_bundle(path/'private'/f'{bundle.record_hash()}.json') == bundle
    assert adapter(tmp_path/'replay',SDK()).replay_offline(path).status == 'registered'
    (path/'payload.json').write_text('{}')
    with pytest.raises(ValueError,match='offline_integrity'):
        load_offline(path)


def test_remote_failure_not_experiment_success(tmp_path,evidence):
    _,bundle=evidence(); sdk=SDK('evaluation'); a=adapter(tmp_path,sdk)
    result=a.publish_experiment(bundle)
    assert result.status == 'failed' and result.experiment_id is not None
    assert result.error_code == 'remote_unavailable'
    # Uncertain successful mutations are never silently repeated.
    assert a.publish_experiment(bundle).status == 'failed'
    assert len(sdk.runs) == 1 and len(sdk.exps) == 1


def test_registration_redacts_holdout_and_private_data(tmp_path,evidence):
    ds,bundle=evidence('holdout'); sdk=SDK(); a=adapter(tmp_path,sdk)
    result=a.publish_experiment(bundle)
    assert result.status == 'registered' and result.detail == 'aggregate_only'
    wire=json.dumps({'datasets':sdk.uploads,'experiments':sdk.exps},default=str)
    # Explicit per-example holdout data and labels cannot enter a general project.
    for example in ds.iter_examples():
        assert example.example_id not in wire and example.event_group not in wire
    assert 'fixture-source' not in wire and 'target_behavior' not in wire
    assert not sdk.runs and not sdk.evals
    exported=a.export_offline(bundle,tmp_path/'holdout')
    public=(exported/'payload.json').read_text()
    assert 'foundation-holdout' not in public and 'reference' not in public
    assert load_offline(exported).bundle.records == bundle.records
    assert not (exported/'private').stat().st_mode & 0o077
    assert not next((exported/'private').glob('*.json')).stat().st_mode & 0o077


def test_two_projects_same_trace_parent_ids(tmp_path):
    telemetry=Telemetry(tmp_path/'t.db',exporter=object())
    with telemetry.story_context(uuid4(),uuid4()):
        with telemetry.span('agent.invoke','finance-media-news-scout',{'agent.name':'foundation'}):
            pass
    records=telemetry.pending_records()
    child=next(r for r in records if r.project.endswith('news-scout'))
    parent=next(r for r in records if r.project.endswith('orchestrator'))
    assert child.trace_id == parent.trace_id and child.parent_span_id == parent.span_id
    assert readable(child).resource.attributes['openinference.project.name'] == 'finance-media-news-scout'
    telemetry.shutdown(0)


def test_replayed_span_duplicate_behavior_documented(tmp_path):
    telemetry=Telemetry(tmp_path/'t.db',exporter=object())
    with telemetry.span('agent.invoke','finance-media-news-scout',{}):
        pass
    record=telemetry.pending_records()[0]
    assert telemetry.enqueue(record).accepted
    assert len(telemetry.pending_records()) == 1
    assert readable(record).context.span_id == int(record.span_id,16)
    telemetry.shutdown(0)


def test_safe_sdk_transport_ignores_ambient_credentials(tmp_path,monkeypatch):
    for key in ['PHOENIX_API_KEY','PHOENIX_CLIENT_HEADERS','HTTP_PROXY','HTTPS_PROXY']:
        monkeypatch.setenv(key,'SECRET')
    monkeypatch.chdir(tmp_path)
    (tmp_path/'.env.phoenix').write_text('PHOENIX_API_KEY=SECRET\nPHOENIX_COLLECTOR_ENDPOINT=https://bad.invalid')
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={'data':[]})
    sdk=safe_client('http://127.0.0.1:6006/v1/traces',transport=httpx.MockTransport(respond))
    sdk._client.cookies.set('session','SECRET')
    sdk._client.headers['Authorization']='SECRET'
    sdk._client.get('v1/projects')
    request=requests[0]
    assert request.url.host == '127.0.0.1'
    assert 'SECRET' not in str(request.headers) and 'cookie' not in request.headers
    with pytest.raises(ValueError):
        sdk._client.get('https://bad.invalid/')
    for bad in ['https://127.0.0.1:6006/v1/traces','http://localhost:6006/v1/traces','http://127.0.0.1:6006/v1/traces?secret=1']:
        with pytest.raises(ValueError):
            safe_client(bad)


def test_offline_rejects_missing_scores_even_with_rehashed_manifest(tmp_path,evidence):
    import hashlib
    from app.evals.run import canonical
    _,bundle=evidence(); a=adapter(tmp_path,SDK())
    path=a.export_offline(bundle,tmp_path/'offline')
    payload=json.loads((path/'payload.json').read_bytes())
    payload['runs'][0]['evaluations']=[]
    raw=canonical(payload); (path/'payload.json').write_bytes(raw)
    manifest=json.loads((path/'manifest.json').read_bytes())
    manifest['files']['payload.json']=hashlib.sha256(raw).hexdigest()
    del manifest['manifest_hash']
    manifest['manifest_hash']=hashlib.sha256(canonical(manifest)).hexdigest()
    (path/'manifest.json').write_bytes(canonical(manifest))
    with pytest.raises(ValueError,match='offline_integrity'):
        load_offline(path)


def test_wire_rejects_private_trace_strings(tmp_path,evidence):
    _,bundle=evidence()
    first=bundle.records[0]
    output=first.output.model_copy(update={'trace_id':'SECRET private trace','span_id':'SECRET'})
    rows=(first.model_copy(update={'output':output}),*bundle.records[1:])
    forged=bundle.model_copy(update={'records':rows})
    sdk=SDK()
    result=adapter(tmp_path,sdk).publish_experiment(forged)
    assert 'SECRET' not in json.dumps(sdk.runs,default=str)
    assert result.status == 'registered'


def test_real_sdk_serializes_existing_runs_and_annotations(tmp_path,evidence):
    _,bundle=evidence(); seen=[]; uploaded={}
    def server(request):
        path=request.url.path
        body=json.loads(request.content) if request.content else None
        seen.append((request.method,path,body))
        if path == '/v1/datasets/upload':
            uploaded.update(body)
            return httpx.Response(200,json={'data':{'dataset_id':'RGF0YXNldDox','version_id':'VmVyc2lvbjox'}})
        if path == '/v1/datasets/RGF0YXNldDox/examples':
            examples=[{'id':f'ZXhhbXBsZTo{i}','input':v,'output':uploaded['outputs'][i],
                'metadata':uploaded['metadata'][i],'updated_at':'2026-11-11T00:00:00Z'}
                for i,v in enumerate(uploaded['inputs'])]
            return httpx.Response(200,json={'data':{'version_id':'VmVyc2lvbjox','examples':examples}})
        if path == '/v1/datasets/RGF0YXNldDox':
            return httpx.Response(200,json={'data':{'id':'RGF0YXNldDox','name':uploaded['name']}})
        if path.endswith('/experiments'):
            return httpx.Response(200,json={'data':{'id':'RXhwZXJpbWVudDox'}})
        if path.endswith('/runs'):
            return httpx.Response(200,json={'data':{'id':'cnVu'+str(len(seen))}})
        if path == '/v1/experiment_evaluations':
            return httpx.Response(200,json={'data':{'id':'ZXZhb'+str(len(seen))}})
        raise AssertionError(path)
    sdk=safe_client(transport=httpx.MockTransport(server))
    a=adapter(tmp_path,sdk)
    result=a.publish_experiment(bundle)
    assert result.status == 'registered'
    runs=[body for _,path,body in seen if path.endswith('/runs')]
    evaluations=[body for _,path,body in seen if path == '/v1/experiment_evaluations']
    assert len(runs) == 3 and len(evaluations) == 18
    assert [run['dataset_example_id'] for run in runs] == ['ZXhhbXBsZTo0','ZXhhbXBsZTo1','ZXhhbXBsZTo2']
    assert runs[0]['start_time'] == bundle.records[0].started_at.isoformat()
    assert {e['name'] for e in evaluations} == {'contract','policy','evidence','state','metadata','fake_rubric'}
    assert all(e['result'] == {'score':1.0,'label':'PASS','explanation':'fixture_check'} for e in evaluations)
    assert {e['experiment_run_id'] for e in evaluations} == {'cnVu5','cnVu12','cnVu19'}


def test_unreachable_actual_sdk_keeps_replayable_evidence(tmp_path,evidence):
    _,bundle=evidence()
    def unavailable(request):
        raise httpx.ConnectError('SECRET',request=request)
    a=adapter(tmp_path,safe_client(transport=httpx.MockTransport(unavailable)))
    path=a.export_offline(bundle,tmp_path/'offline')
    assert a.publish_experiment(bundle).status == 'failed'
    assert load_offline(path).bundle == bundle
    # A refused connection is safe to retry: no mutation reached a server.
    good=adapter(tmp_path,SDK())
    assert good.replay_offline(path).status == 'registered'


def test_cli_phoenix_failure_still_prints_local_bundle(tmp_path,monkeypatch,capsys):
    from app.evals.run import main
    monkeypatch.setattr('app.evals.phoenix.safe_client',lambda *a,**k:SDK('dataset'))
    code=main(['--agent','foundation','--dataset','evals/foundation/1.0.0','--split','dev',
        '--output',str(tmp_path),'--phoenix'])
    summary=json.loads(capsys.readouterr().out)
    assert code == 2 and summary['status'] == 'PASS'
    assert summary['phoenix']['status'] == 'failed'
    assert Path(summary['bundle_path']).is_file()
    assert load_offline(Path(summary['offline_path'])).bundle.status == 'PASS'


def test_missing_observed_times_never_fabricates_remote_run_dates(tmp_path,evidence):
    _,bundle=evidence()
    first=bundle.records[0].model_copy(update={'started_at':None,'finished_at':None})
    missing=bundle.model_copy(update={'records':(first,*bundle.records[1:])})
    sdk=SDK(); result=adapter(tmp_path,sdk).publish_experiment(missing)
    assert result.status == 'failed' and result.error_code == 'observed_times_missing'
    assert not sdk.uploads and not sdk.exps and not sdk.runs



def test_publish_always_preserves_offline_before_first_remote_mutation(tmp_path,evidence):
    _,bundle=evidence()
    sdk=SDK('dataset'); a=adapter(tmp_path,sdk)
    result=a.publish_experiment(bundle)
    assert result.status == 'failed'
    assert load_offline(tmp_path/'offline'/bundle.record_hash()).bundle == bundle


def test_upload_success_followed_by_readback_connect_failure_is_ambiguous(tmp_path,evidence):
    _,bundle=evidence(); uploads=[]
    def server(request):
        if request.method == 'POST':
            uploads.append(request)
            return httpx.Response(200,json={'data':{'dataset_id':'RGF0YXNldDox','version_id':'VmVyc2lvbjox'}})
        raise httpx.ConnectError('SECRET',request=request)
    a=adapter(tmp_path,safe_client(transport=httpx.MockTransport(server)))
    assert a.publish_experiment(bundle).status == 'failed'
    second=a.publish_experiment(bundle)
    assert second.status == 'failed' and second.error_code == 'remote_state_unknown'
    assert len(uploads) == 1


@pytest.mark.parametrize('mutation',['split','severity','numeric'])
def test_dataset_registration_rechecks_original_source_and_split(tmp_path,evidence,mutation):
    ds,_=evidence('holdout' if mutation == 'split' else 'dev')
    if mutation == 'split':
        ds=ds.model_copy(update={'split':'dev'})
    else:
        ds=ds.model_copy(deep=True)
        first=next(ds.iter_examples())
        if mutation == 'severity':
            ds._examples=(first.model_copy(update={'severity':'PRIVATE severity'}),*ds._examples[1:])
        else:
            value=first.input.data.model_copy(update={'value':90000001})
            modified=first.model_copy(update={'input':first.input.model_copy(update={'data':value})})
            ds._examples=(modified,*ds._examples[1:])
    sdk=SDK(); a=adapter(tmp_path,sdk)
    with pytest.raises(ValueError,match='dataset_identity'):
        a.register_dataset(ds)
    assert not sdk.uploads
    import sqlite3
    with sqlite3.connect(tmp_path/'registry.db') as db:
        assert db.execute('SELECT count(*) FROM phoenix_operations').fetchone()[0] == 0


def test_copied_holdout_bundle_cannot_be_projected_as_dev(tmp_path,evidence):
    _,bundle=evidence('holdout')
    forged=bundle.model_copy(update={'split':'dev'})
    sdk=SDK(); a=adapter(tmp_path,sdk)
    with pytest.raises(ValueError,match='bundle_dataset_identity'):
        a.export_offline(forged,tmp_path/'forged')
    with pytest.raises(ValueError,match='bundle_dataset_identity'):
        a.publish_experiment(forged)
    assert not sdk.uploads and not sdk.exps
    assert not (tmp_path/'forged'/'payload.json').exists()


def test_unknown_metric_text_stays_private_and_public_status_nonpassing(tmp_path,evidence):
    from app.evals.run import aggregate,assess
    _,bundle=evidence(); first=bundle.records[0]
    abnormal=first.results[0].model_copy(update={'metric':'PRIVATE arbitrary metric'})
    rows=(first.model_copy(update={'results':(abnormal,*first.results[1:])}),*bundle.records[1:])
    status,critical,issues=assess(rows,bundle.expected_count,bundle.config.repeats,())
    invalid=bundle.model_copy(update={'records':rows,'status':status,'critical_false_accepts':critical,
        'issues':issues,'aggregates':aggregate(rows)})
    sdk=SDK(); a=adapter(tmp_path,sdk)
    path=a.export_offline(invalid,tmp_path/'abnormal')
    evidence=load_offline(path)
    assert evidence.payload['status'] == 'INCOMPLETE'
    assert 'PRIVATE' not in (path/'payload.json').read_text()
    assert 'PRIVATE' in next((path/'private').glob('[0-9a-f]'*64+'.json')).read_text()
    assert a.publish_experiment(invalid).status == 'registered'
    assert 'PRIVATE' not in json.dumps(sdk.evals,default=str)
    assert sdk.exps[0]['experiment_metadata']['local_status'] == 'INCOMPLETE'


def test_verified_export_retry_does_not_rewrite_artifacts(tmp_path,evidence,monkeypatch):
    _,bundle=evidence(); a=adapter(tmp_path,SDK()); path=a.export_offline(bundle,tmp_path/'immutable')
    original={p.relative_to(path):p.read_bytes() for p in path.rglob('*') if p.is_file()}
    def interrupted(*args,**kwargs):
        raise OSError('interrupted artifact write')
    monkeypatch.setattr(Path,'write_bytes',interrupted)
    # A retry must reuse the verified export; a write would simulate interruption.
    assert a.export_offline(bundle,path) == path
    assert load_offline(path).bundle == bundle
    assert original == {p.relative_to(path):p.read_bytes() for p in path.rglob('*') if p.is_file()}


def test_offline_reload_rejects_cross_split_records_with_rehashed_files(tmp_path,evidence):
    from app.evals.run import canonical,persist_bundle
    import hashlib
    _,dev=evidence(); _,holdout=evidence('holdout')
    forged=holdout.model_copy(update={'split':'dev'})
    path=adapter(tmp_path,SDK()).export_offline(dev,tmp_path/'forged-reload')
    old=path/'private'/f'{dev.record_hash()}.json'
    old.unlink()
    saved=persist_bundle(forged,path/'private')
    payload=json.loads((path/'payload.json').read_bytes())
    payload['bundle_hash']=forged.record_hash()
    # Independently construct the previously accepted wire form, as an external
    # corrupted exporter could. Public hash updates cannot authenticate split IDs.
    payload['runs']=[{'opaque_id':hashlib.sha256(canonical([forged.provenance['dataset_hash'],r.example_id])).hexdigest(),
        'repeat':r.repeat,'output':{'status':r.output.status,'value':r.output.output.value if r.output.output else None},
        'error':r.error,'started_at':r.started_at.isoformat(),'finished_at':r.finished_at.isoformat(),
        'trace_id':r.output.trace_id,'span_id':r.output.span_id,
        'evaluations':[result.model_dump(mode='json') for result in r.results]} for r in forged.records]
    (path/'payload.json').write_bytes(canonical(payload))
    core={'schema_version':'1.0','bundle_hash':forged.record_hash(),'registration_status':'pending',
        'files':{str(p.relative_to(path)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [path/'payload.json',saved,path/'private/dataset-manifest.json',path/'private/examples.jsonl']}}
    (path/'manifest.json').write_bytes(canonical({**core,'manifest_hash':hashlib.sha256(canonical(core)).hexdigest()}))
    with pytest.raises(ValueError,match='offline_integrity'):
        load_offline(path)


def test_source_pin_change_cannot_poison_dataset_registration(tmp_path):
    import shutil
    root=tmp_path/'dataset'
    shutil.copytree('evals/foundation/1.0.0',root)
    loaded=load_dataset(root,'dev','gate')
    assert str(root) not in loaded.model_dump_json() and str(root) not in repr(loaded)
    (root/'dev.jsonl').write_bytes((root/'dev.jsonl').read_bytes()+b'\n')
    sdk=SDK()
    with pytest.raises(ValueError,match='dataset_identity'):
        adapter(tmp_path,sdk).register_dataset(loaded)
    assert not sdk.uploads


def test_valid_incomplete_subset_still_exports_and_replays(tmp_path,evidence):
    from app.evals.run import aggregate,aggregate_usage,assess
    _,bundle=evidence(); rows=bundle.records[:1]
    status,critical,issues=assess(rows,bundle.expected_count,bundle.config.repeats,())
    incomplete=bundle.model_copy(update={'records':rows,'status':status,'critical_false_accepts':critical,
        'issues':issues,'aggregates':aggregate(rows),'usage':aggregate_usage(rows)})
    sdk=SDK(); a=adapter(tmp_path,sdk)
    path=a.export_offline(incomplete,tmp_path/'subset')
    assert load_offline(path).payload['status'] == 'INCOMPLETE'
    assert a.replay_offline(path).status == 'registered'
    assert len(sdk.runs) == 1 and len(sdk.evals) == 6


def test_interrupted_fresh_export_never_publishes_partial_directory(tmp_path,evidence,monkeypatch):
    _,bundle=evidence(); a=adapter(tmp_path,SDK()); destination=tmp_path/'pending'
    write=Path.write_bytes
    def interrupt(path,data):
        if path.name == 'manifest.json':
            raise OSError('interrupted before completion')
        return write(path,data)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'write_bytes',interrupt)
        with pytest.raises(OSError,match='interrupted'):
            a.export_offline(bundle,destination)
    assert not destination.exists()
    assert a.export_offline(bundle,destination) == destination
    assert load_offline(destination).bundle == bundle


def test_concurrent_reader_sees_complete_export_or_absence(tmp_path,evidence,monkeypatch):
    import threading
    _,bundle=evidence(); a=adapter(tmp_path,SDK()); destination=tmp_path/'concurrent'
    write=Path.write_bytes; staged=threading.Event(); release=threading.Event(); errors=[]
    def pause(path,data):
        result=write(path,data)
        if path.name == 'manifest.json':
            staged.set()
            if not release.wait(5):
                raise RuntimeError('test synchronization timeout')
        return result
    def export():
        try:
            a.export_offline(bundle,destination)
        except Exception as error:
            errors.append(error)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'write_bytes',pause)
        thread=threading.Thread(target=export)
        thread.start()
        try:
            assert staged.wait(5)
            assert not destination.exists()
        finally:
            release.set()
            thread.join(5)
    assert not thread.is_alive() and not errors
    assert load_offline(destination).bundle == bundle


def test_export_publication_lock_releases_after_process_death(tmp_path,evidence):
    import subprocess
    import sys
    from app.evals.run import persist_bundle
    _,bundle=evidence(); saved=persist_bundle(bundle,tmp_path/'bundles'); destination=tmp_path/'crash'
    script='''
import os,sys
from pathlib import Path
from app.evals.phoenix import PhoenixAdapter
from app.evals.run import read_bundle
bundle=read_bundle(Path(sys.argv[1]))
adapter=PhoenixAdapter(Path(sys.argv[2]))
os.rename=lambda *args: os._exit(23)
adapter.export_offline(bundle,Path(sys.argv[3]))
'''
    stopped=subprocess.run([sys.executable,'-c',script,str(saved),str(tmp_path/'registry.db'),str(destination)],
        capture_output=True,timeout=10)
    assert stopped.returncode == 23 and not destination.exists(), stopped.stderr
    # The next exporter must not deadlock on a lock left by the dead writer.
    retried=subprocess.run([sys.executable,'-c',
        'from pathlib import Path; import sys; from app.evals.phoenix import PhoenixAdapter; '
        'from app.evals.run import read_bundle; '
        'PhoenixAdapter(Path(sys.argv[2])).export_offline(read_bundle(Path(sys.argv[1])),Path(sys.argv[3]))',
        str(saved),str(tmp_path/'registry.db'),str(destination)],capture_output=True,timeout=10)
    assert retried.returncode == 0, retried.stderr
    assert load_offline(destination).bundle == bundle


@pytest.mark.parametrize('split',['dev','holdout'])
def test_public_aggregates_match_projected_outcomes_and_remote_metadata(tmp_path,evidence,split):
    from app.evals.run import aggregate,assess
    _,bundle=evidence(split)
    first=bundle.records[0]
    malformed=first.results[0].model_copy(update={'metric':'PRIVATE malformed metric'})
    records=(first.model_copy(update={'results':(malformed,*first.results[1:])}),*bundle.records[1:])
    status,critical,issues=assess(records,bundle.expected_count,bundle.config.repeats,())
    diagnostic=bundle.model_copy(update={'records':records,'status':status,
        'critical_false_accepts':critical,'issues':issues,'aggregates':aggregate(records)})
    sdk=SDK(); a=adapter(tmp_path,sdk)
    path=a.export_offline(diagnostic,tmp_path/'projected')
    exported=load_offline(path)
    # The projected malformed metric becomes ERROR with no numeric denominator;
    # the two observed passes contribute the two valid denominator observations.
    want={'samples':3,'denominator':2,'passes':2,'failures':0,'errors':1,'skipped':0,'rate':1.0}
    assert exported.payload['aggregates']['contract'] == want
    assert exported.payload['status'] == 'INCOMPLETE'
    assert exported.bundle == diagnostic
    assert exported.bundle.aggregates['contract'].passes == 3
    assert exported.bundle.aggregates['contract'].errors == 0
    assert 'PRIVATE' not in (path/'payload.json').read_text()
    assert a.publish_experiment(diagnostic).status == 'registered'
    metadata=sdk.exps[0]['experiment_metadata']
    assert metadata['aggregates']['contract'] == want
    assert metadata['local_status'] == 'INCOMPLETE'
    if split == 'dev':
        annotations=[r for r in sdk.evals if r['name'] == 'contract']
        assert [r['label'] for r in annotations] == ['ERROR','PASS','PASS']
        assert sum(r['label'] == 'PASS' for r in annotations) == metadata['aggregates']['contract']['passes']
        assert sum(r['label'] == 'ERROR' for r in annotations) == metadata['aggregates']['contract']['errors']
    else:
        assert exported.payload['runs'] == [] and not sdk.runs and not sdk.evals
