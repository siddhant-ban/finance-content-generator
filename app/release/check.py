"""Independently verify pinned local evidence; CLI only reports readiness."""
import argparse
import inspect
import json
import os
from pathlib import Path
from app.config import Settings
from app.execution.fakes import FakeStage
from app.evals.compare import compare
from app.evals.datasets import DatasetSnapshot,load_dataset_snapshot,read_dataset_snapshot
from app.evals.evaluators import REQUIRED, foundation_evaluators
from app.evals.models import EvalExample, FixtureInput, ExperimentBundle
from app.contracts.execution import InputEnvelope
from app.prompts import load_prompt
from app.evals.phoenix import _bind_bundle, load_offline
from app.evals.run import canonical,digest,load_policy,read_bundle
from app.release.models import ReleaseCandidate,ReleasePolicy

SPLITS=('dev','holdout','prod_regression')


def installed_evaluator_pins():
    evaluators=foundation_evaluators()
    fingerprint=digest(canonical([(e.evaluator_id,e.version,e.fingerprint(),digest(inspect.getsource(type(e)).encode())) for e in evaluators]))
    return evaluators,fingerprint,digest(Path('app/evals/evaluators.py').read_bytes())


def read_evidence(pin):
    """Capture selected evidence once; no source reads after its verification."""
    path=Path(pin.path)
    if path.is_dir():
        verified=load_offline(path)
        bundle=verified.bundle
        header_bytes,content=verified.manifest_bytes,verified.examples_bytes
    else:
        bundle=read_bundle(path)
        snapshot=read_dataset_snapshot(Path(bundle.config.dataset_root))
        dataset=load_dataset_snapshot(snapshot,bundle.split,'gate')
        _bind_bundle(bundle,dataset)
        header_bytes=snapshot.manifest_bytes
        content=dict(snapshot.split_bytes)[bundle.split]
    if bundle.record_hash()!=pin.bundle_hash:
        raise ValueError('bundle_pin_changed')
    return bundle,header_bytes,content


def intrinsic(candidate):
    """Recompute each required metric per example/repeat; aggregates cannot conceal failures."""
    if candidate.policy!=ReleasePolicy() or set(candidate.evidence)!=set(SPLITS):
        raise ValueError('required_splits_or_policy')
    evaluators,evaluator_hash,implementation_hash=installed_evaluator_pins()
    schema_hash=digest(canonical({'input':InputEnvelope[FixtureInput].model_json_schema(),
        'fixture':FixtureInput.model_json_schema(),'example':EvalExample.model_json_schema(),
        'experiment':ExperimentBundle.model_json_schema()}))
    prompt=load_prompt(Path('prompts/manifests/foundation.v1.json'))
    bundles={}; reasons=[]
    common=None
    captured={split:read_evidence(candidate.evidence[split]) for split in SPLITS}
    # Full global validation is identical for regular, offline and mixed paths.
    snapshot=DatasetSnapshot(captured['dev'][1],tuple((split,captured[split][2]) for split in SPLITS))
    for split in SPLITS:
        bundle=captured[split][0]
        dataset=load_dataset_snapshot(snapshot,split,'gate')
        _bind_bundle(bundle,dataset)
        bundles[split]=bundle
        pins=bundle.provenance
        if bundle.split!=split or dataset.kind!='synthetic_foundation' or bundle.config.agent_name!=candidate.agent_name:
            raise ValueError('dataset_scope')
        policy=load_policy(bundle.config.policy_path)
        if (pins['schema_hash']!=schema_hash or pins['prompt_hash']!=prompt.sha256
                or pins['prompt_version']!=prompt.prompt_version
                or bundle.model_configuration.provider!='fake'
                or pins['policy_hash']!=policy.record_hash() or pins['policy_version']!=policy.policy_id
                or pins['evaluator_hash']!=evaluator_hash or pins['evaluator_implementation_hash']!=implementation_hash
                or bundle.evaluator_versions!={e.evaluator_id:e.version for e in evaluators}):
            raise ValueError('policy_evaluator_or_calibration_pin')
        if common is None:
            common=pins
        elif common!=pins:
            raise ValueError('release_configuration_mismatch')
        examples={e.example_id:e for e in dataset.iter_examples()}
        expected={(key,repeat) for key in examples for repeat in range(bundle.config.repeats)}
        if (not expected or {(r.example_id,r.repeat) for r in bundle.records}!=expected
                or len(bundle.records)!=len(expected) or bundle.expected_count!=len(examples)):
            reasons.append('incomplete_split')
        if bundle.status!='PASS' or bundle.critical_false_accepts != 0 or bundle.issues:
            reasons.append('evaluation_failed')
        for row in bundle.records:
            example=examples[row.example_id]
            if row.output is not None and (row.output.agent_version!=FakeStage.agent_version
                    or row.output.prompt_id!=prompt.prompt_id
                    or row.output.config_version!=Settings().config_version):
                raise ValueError('unexecuted_stage_version')
            if (row.output is None or row.error or set(r.evaluator_id for r in row.results)!=set(REQUIRED)
                    or len(row.results)!=len(REQUIRED)):
                reasons.append('missing_evaluations'); continue
            # Evidence status alone cannot establish deterministic correctness.
            derived=tuple(result for evaluator in evaluators for result in evaluator.evaluate(example,row.output))
            actual={r.evaluator_id:r for r in row.results}
            if any(actual[result.evaluator_id]!=result for result in derived):
                reasons.append('evaluation_evidence_mismatch')
            if any(r.status!='PASS' or r.value!=1.0 or r.denominator!=1 for r in derived):
                reasons.append('per_repeat_threshold')
    return bundles,tuple(sorted(set(reasons)))


def baseline_reasons(bundles,baseline):
    return () if all(compare(bundles[split],baseline[split]).status=='PASS' for split in SPLITS) else ('incomparable_or_failed_baseline',)


def main(argv=None):
    from app.config import Settings
    from app.release.service import ReleaseService
    from app.storage.db import migrate,open_database
    parser=argparse.ArgumentParser(description='Report synthetic readiness; human approval has no CLI.')
    parser.add_argument('--agent',required=True,choices=['foundation'])
    parser.add_argument('--candidate',required=True,type=Path)
    parser.add_argument('--registry',type=Path,default=None)
    args=parser.parse_args(argv)
    conn=None
    try:
        candidate=ReleaseCandidate.model_validate_json(args.candidate.read_bytes())
        registry=args.registry or Settings.from_env().data_dir/'state.db'
        registry.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(registry,os.O_CREAT | os.O_WRONLY,0o600); os.close(fd)
        conn=open_database(registry); migrate(conn)
        result=ReleaseService(conn).check(candidate)
        print(result.model_dump_json())
        return 0 if result.status=='APPROVED' else 2 if result.status=='ERROR' else 1
    except Exception:
        print(json.dumps({'status':'ERROR','reasons':['invalid_candidate_or_registry']}))
        return 2
    finally:
        if conn is not None: conn.close()

if __name__=='__main__':
    raise SystemExit(main())
