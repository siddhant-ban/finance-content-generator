"""Safety gates must derive readiness from complete pinned evidence."""
import json
from pathlib import Path
import pytest
from tests.release_fixtures import evidence, candidate_for
from app.release.service import ReleaseService
from app.release.models import EvidencePin
from app.storage.db import migrate, open_database
from app.evals.run import aggregate, assess, persist_bundle

@pytest.fixture
def service(tmp_path):
    conn=open_database(tmp_path/'release.db'); migrate(conn)
    yield ReleaseService(conn)
    conn.close()

def test_no_baseline_requires_human_bootstrap(service,evidence,tmp_path):
    decision=service.check(candidate_for(evidence,tmp_path/'bundles'))
    assert decision.status == 'REQUIRES_BOOTSTRAP_APPROVAL'
    assert service.active('foundation') is None

@pytest.mark.parametrize('change',['split','evaluator','calibration','judge_only','repeat','critical'])
def test_missing_split_evaluator_or_calibration_blocks(service,evidence,tmp_path,change):
    if change == 'split':
        evidence.pop('holdout')
    else:
        bundle=evidence['holdout']; first=bundle.records[0]
        if change == 'calibration':
            # A coherent bundle from another calibration can persist, but the
            # installed release policy must still reject it.
            from app.evals.run import canonical, digest
            pins=tuple((name,version,'a'*64 if name=='fake_rubric' else fingerprint,implementation)
                       for name,version,fingerprint,implementation in bundle.evaluator_pins)
            evidence['holdout']=bundle.model_copy(update={'evaluator_pins':pins,
                'provenance':{**bundle.provenance,'evaluator_hash':digest(canonical(pins))}})
        else:
            results=first.results
            if change in ('evaluator','judge_only'):
                results=results[:-1] if change=='evaluator' else results[-1:]
            else:
                results=(results[0].model_copy(update={'status':'FAIL','value':0.0}),*results[1:])
            rows=(first.model_copy(update={'results':results}),*bundle.records[1:])
            if change=='critical':
                critical=next(row for row in rows if row.expected_outcome=='reject')
                output=first.output
                rows=tuple(row.model_copy(update={'output':output}) if row is critical else row for row in rows)
            status,critical,issues=assess(rows,bundle.expected_count,bundle.config.repeats,())
            evidence['holdout']=bundle.model_copy(update={'records':rows,'status':status,'critical_false_accepts':critical,'issues':issues,'aggregates':aggregate(rows)})
    assert service.check(candidate_for(evidence,tmp_path/'bundles')).status in {'BLOCKED','ERROR'}

def test_changed_bundle_or_configuration_revokes_eligibility(service,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles')
    assert service.check(candidate).status=='REQUIRES_BOOTSTRAP_APPROVAL'
    Path(candidate.evidence['dev'].path).write_text('{}')
    assert service.check(candidate).status=='ERROR'

def test_synthetic_release_cannot_enable_real_agent(service,evidence,tmp_path):
    candidate=candidate_for(evidence,tmp_path/'bundles')
    service.check(candidate)
    with pytest.raises(ValueError):
        service.require_approved('researcher', {}, validators_ready=True)

def test_complete_offline_export_allowed_missing_evals_denied(service,evidence,tmp_path):
    from app.evals.phoenix import PhoenixAdapter
    import shutil
    copied=tmp_path/'dataset'
    shutil.copytree('evals/foundation/1.0.0',copied)
    evidence={split:bundle.model_copy(update={'config':bundle.config.model_copy(update={'dataset_root':str(copied)}),
        'provenance':{**bundle.provenance,'experiment_config_hash':bundle.config.model_copy(update={'dataset_root':str(copied)}).record_hash()}})
        for split,bundle in evidence.items()}
    adapter=PhoenixAdapter(tmp_path/'phoenix.db',client=object())
    candidate=candidate_for(evidence,tmp_path/'bundles')
    pins={split:EvidencePin(path=str(adapter.export_offline(bundle,tmp_path/'offline'/split)),
        bundle_hash=bundle.record_hash()) for split,bundle in evidence.items()}
    offline=candidate.model_copy(update={'evidence':pins})
    shutil.rmtree(copied)
    assert service.check(offline).status=='REQUIRES_BOOTSTRAP_APPROVAL'
    (Path(pins['holdout'].path)/'private/examples.jsonl').unlink()
    assert service.check(offline).status=='ERROR'


def test_changed_schema_pin_cannot_self_attest_readiness(service,evidence,tmp_path):
    altered={split:bundle.model_copy(update={'provenance':{**bundle.provenance,'schema_hash':'a'*64}})
        for split,bundle in evidence.items()}
    assert service.check(candidate_for(altered,tmp_path/'bundles')).status=='ERROR'


def test_release_schemas_are_versioned(tmp_path):
    from app.contracts.export import export_schemas
    assert 'releases.json' in export_schemas(tmp_path)
    assert (tmp_path/'releases.json').read_bytes()==Path('schemas/v1/releases.json').read_bytes()


@pytest.mark.parametrize('field,value',[('agent_version','unexecuted.v9'),('prompt_id','unexecuted'),('config_version','unexecuted')])
def test_unexecuted_stage_versions_cannot_be_approved(service,evidence,tmp_path,field,value):
    forged={split:bundle.model_copy(update={'records':tuple(row.model_copy(update={
        'output':row.output.model_copy(update={field:value})}) for row in bundle.records)})
        for split,bundle in evidence.items()}
    assert service.check(candidate_for(forged,tmp_path/'bundles')).status=='ERROR'


def test_offline_gate_uses_verified_snapshot_after_source_changes(service,evidence,tmp_path,monkeypatch):
    import app.release.check as gate
    from app.evals.phoenix import PhoenixAdapter
    adapter=PhoenixAdapter(tmp_path/'phoenix.db',client=object())
    candidate=candidate_for(evidence,tmp_path/'bundles')
    pins={split:EvidencePin(path=str(adapter.export_offline(bundle,tmp_path/'offline'/split)),bundle_hash=bundle.record_hash()) for split,bundle in evidence.items()}
    original=gate.load_offline
    def verified_then_changed(path):
        verified=original(path)
        (path/'private/examples.jsonl').write_bytes(b'changed after verification')
        return verified
    monkeypatch.setattr(gate,'load_offline',verified_then_changed)
    assert service.check(candidate.model_copy(update={'evidence':pins})).status=='REQUIRES_BOOTSTRAP_APPROVAL'


def test_offline_verification_never_rereads_checked_artifacts(evidence,tmp_path,monkeypatch):
    from app.evals.phoenix import PhoenixAdapter,load_offline
    path=PhoenixAdapter(tmp_path/'phoenix.db',client=object()).export_offline(evidence['dev'],tmp_path/'offline')
    original=Path.read_bytes; reads={}
    def changing_storage(file):
        if file.is_relative_to(path) and file.name!='manifest.json':
            reads[file]=reads.get(file,0)+1
            if reads[file]>1:
                return b'changed after hash verification'
        return original(file)
    monkeypatch.setattr(Path,'read_bytes',changing_storage)
    assert load_offline(path).bundle.record_hash()==evidence['dev'].record_hash()


@pytest.mark.parametrize('format',['regular','offline','mixed'])
@pytest.mark.parametrize('violation',['event','evidence','identity','adjudication','reference','rights'])
def test_dataset_governance_blocked_for_every_evidence_format(service,evidence,tmp_path,format,violation):
    import shutil
    from app.evals.models import DatasetManifest,LoadedDataset,EvalExample
    from app.evals.phoenix import _wire_payload
    from app.evals.run import canonical,digest
    root=tmp_path/'leaking'; shutil.copytree('evals/foundation/1.0.0',root)
    header=json.loads((root/'manifest.json').read_bytes())
    dev=[json.loads(line) for line in (root/'dev.jsonl').read_bytes().splitlines()]
    holdout=[json.loads(line) for line in (root/'holdout.jsonl').read_bytes().splitlines()]
    if violation=='event': holdout[0]['event_group']=dev[0]['event_group']
    elif violation=='evidence': holdout[0]['evidence_fingerprints']=dev[0]['evidence_fingerprints']
    elif violation=='identity': holdout[0]['agent_version']='unexecuted'
    elif violation=='adjudication': holdout[0]['adjudication']['status']='human_adjudicated'
    elif violation=='reference': holdout[0]['reference_ids']=['missing-example']
    elif violation=='rights': holdout[0]['rights']['status']='reviewed_permitted'
    (root/'holdout.jsonl').write_bytes(b''.join(canonical(row)+b'\n' for row in holdout))
    header['files']['holdout']['event_groups']=list(dict.fromkeys(row['event_group'] for row in holdout))
    header['files']['holdout']['evidence_fingerprints']=list(dict.fromkeys(fp for row in holdout for fp in row['evidence_fingerprints']))
    header['files']['holdout']['sha256']=digest((root/'holdout.jsonl').read_bytes())
    manifest=DatasetManifest.model_validate_json(json.dumps(header))
    (root/'manifest.json').write_bytes(canonical(header))
    pins={}
    for split,bundle in evidence.items():
        config=bundle.config.model_copy(update={'dataset_root':str(root)})
        bundle=bundle.model_copy(update={'config':config,'provenance':{**bundle.provenance,
            'dataset_hash':manifest.record_hash(),'experiment_config_hash':config.record_hash()}})
        if format=='regular' or (format=='mixed' and split=='dev'):
            pins[split]=EvidencePin(path=str(persist_bundle(bundle,tmp_path/'bundles')),bundle_hash=bundle.record_hash()); continue
        exported=tmp_path/'offline'/split; (exported/'private').mkdir(parents=True)
        saved=persist_bundle(bundle,exported/'private')
        raw=(root/f'{split}.jsonl').read_bytes()
        selected=tuple(EvalExample.model_validate_json(line) for line in raw.splitlines())
        dataset=LoadedDataset(dataset_version=manifest.dataset_version,dataset_hash=manifest.record_hash(),agent_name='foundation',kind='synthetic_foundation',split=split,purpose='gate',count=len(selected))
        # Public projection omits adjudication and identity metadata; construct it
        # independently of the malicious private rights field. Loading must still
        # reject forbidden private provenance rather than trust the projection.
        dataset._examples=tuple(row.model_copy(update={'rights':row.rights.model_copy(update={'status':'synthetic_fixture'})}) for row in selected)
        files={'private/'+saved.name:saved.read_bytes(),'private/dataset-manifest.json':canonical(header),
            'private/examples.jsonl':raw,'payload.json':canonical(_wire_payload(bundle,dataset))}
        for name,content in files.items(): (exported/name).write_bytes(content)
        core={'schema_version':'1.0','bundle_hash':bundle.record_hash(),'files':{name:digest(content) for name,content in files.items()},'registration_status':'pending'}
        (exported/'manifest.json').write_bytes(canonical({**core,'manifest_hash':digest(canonical(core))}))
        pins[split]=EvidencePin(path=str(exported),bundle_hash=bundle.record_hash())
    if format=='offline': shutil.rmtree(root)
    from app.release.models import ReleaseCandidate
    assert service.check(ReleaseCandidate(evidence=pins)).status=='ERROR'
