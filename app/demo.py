"""Offline synthetic foundation acceptance; never logs in, approves or publishes.

Arithmetic FakeStage execution and hand-authored synthetic editorial artifacts
exercise infrastructure only. These validators cannot validate editorial media.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from collections import Counter
from typing import Literal
from uuid import UUID, uuid4

from app.contracts.common import ArtifactRef, StrictModel
from app.contracts.evidence import ClaimRevision, ClaimUse, EvidenceRef, SourceRecord, VerificationRecord
from app.contracts.execution import AttemptRecord, InputEnvelope, SyntheticData
from app.contracts.workflow import PolicyCheck, PolicyReport, ReviewManifest, RevisionRef, StoryState
from app.evals.datasets import load_dataset
from app.evals.evaluators import foundation_evaluators
from app.evals.models import ExperimentConfig
from app.evals.phoenix import PhoenixAdapter, load_offline
from app.evals.run import canonical, create_runner, digest, persist_bundle, run_experiment
from app.observability.models import SpanRecord
from app.execution.fakes import FakeStage
from app.release.models import EvidencePin, ReleaseCandidate
from app.release.service import ReleaseService
from app.storage.artifacts import ArtifactStore
from app.workflow.policy import ArtifactValidation, AssetLicense, MEMBERS, PolicyEngine
from app.workflow.state import PolicyDenied, StateService, TransitionRequest


class SyntheticArtifact(StrictModel):
    schema_id: Literal['foundation.demo.v1'] = 'foundation.demo.v1'
    kind: Literal['synthetic_foundation'] = 'synthetic_foundation'
    stage: str
    label: str = 'Synthetic foundation fixture; not editorial media'
    runner_output: ArtifactRef | None = None
    decision: Literal['ADVANCE','HOLD','REJECT'] | None = None
    eligible: bool = False
    claims: tuple[ClaimRevision,...] = ()
    sources: tuple[SourceRecord,...] = ()
    verdicts: tuple[VerificationRecord,...] = ()
    uses: tuple[ClaimUse,...] = ()
    licenses: tuple[AssetLicense,...] = ()


class DemoReport(StrictModel):
    schema_version: Literal['1.1'] = '1.1'
    kind: Literal['synthetic_foundation'] = 'synthetic_foundation'
    status: Literal['PASS','FAIL']
    final_state: StoryState
    story_id: UUID
    run_id: UUID
    manifest_hash: str
    manifest_path: str
    member_hashes: dict[str,str]
    member_paths: dict[str,str]
    journal_path: str
    runner_output_path: str
    runner_output_hash: str
    prompt_version: str
    prompt_hash: str
    experiments: dict[str,dict]
    negative_gates: dict[str,dict]
    release_gate: dict
    telemetry: dict
    limitations: tuple[str,...] = ('Synthetic infrastructure only; no agent quality claim.',
        'No login, content approval, release approval or publication performed by demo.',
        'Live OAuth, current Phoenix visibility and remote CI are unverified.')


def review_policy(root: Path, conn):
    """Explicit synthetic-only validators, also usable by a local review embedding."""
    store = ArtifactStore(root/'artifacts', conn)
    policy = PolicyEngine(store)
    for stage in ('candidate','cluster','score',*MEMBERS):
        def validate(data, expected=stage):
            value = SyntheticArtifact.model_validate_json(json.dumps(data))
            if value.stage != expected:
                raise ValueError('synthetic_stage_mismatch')
            return ArtifactValidation(report=PolicyReport(ready=True,
                checks=(PolicyCheck(code='synthetic_schema',passed=True),)),
                decision=value.decision,eligible=value.eligible,claims=value.claims,
                sources=value.sources,verdicts=value.verdicts,uses=value.uses,licenses=value.licenses)
        policy.register(stage,'foundation.demo.v1','synthetic_foundation',validate)
    return store, policy


def synthetic_artifact(store, stage, **fields):
    value = SyntheticArtifact(stage=stage, **fields)
    return store.put(value.canonical_bytes(),'application/json','1.0')


def synthetic_envelope(runner, *, story_id=None, run_id=None):
    value = SyntheticData(value=4)
    return InputEnvelope[SyntheticData](run_id=run_id or uuid4(),story_id=story_id or uuid4(),
        invocation_id=uuid4(),agent_name='foundation',agent_version=FakeStage.agent_version,
        prompt_id=runner.prompt.prompt_id,prompt_version=runner.prompt.prompt_version,
        prompt_hash=runner.prompt.sha256,config_version=runner.settings.config_version,
        config_hash=runner.settings.config_hash(),created_at=datetime.now(timezone.utc),
        input=value,input_ref=runner.store.put(value.canonical_bytes(),'application/json','1.0'))


def synthetic_manifest(store, story_id):
    """Original metadata-only, explicitly fictional evidence, never a fetched claim."""
    source = SourceRecord(source_id=uuid4(),revision=1,
        original_url='https://example.invalid/synthetic-foundation',
        canonical_url='https://example.invalid/synthetic-foundation',publisher='Synthetic fixture',
        source_class='primary',published_at=None,publication_time_reason='Fictional fixture has no publication',
        retrieved_at=datetime.now(timezone.utc),locator='synthetic arithmetic fixture',
        rights='metadata_only',usage_notes='Original synthetic infrastructure test; no factual news claim')
    evidence = EvidenceRef(source_id=source.source_id,source_revision=1,source_record_hash=source.record_hash())
    claim = ClaimRevision(claim_id=uuid4(),revision=1,text='Synthetic fixture: four plus one equals five.',
        claim_type='fact',materiality='material',evidence_refs=(evidence,))
    verdict = VerificationRecord(claim_id=claim.claim_id,claim_revision=1,claim_hash=claim.record_hash(),
        status='VERIFIED',evidence_refs=(evidence,))
    use = ClaimUse(claim_id=claim.claim_id,claim_revision=1,text=claim.text)
    refs = {stage:synthetic_artifact(store,stage) for stage in MEMBERS}
    refs['dossier'] = synthetic_artifact(store,'dossier',claims=(claim,),sources=(source,))
    refs['verification'] = synthetic_artifact(store,'verification',verdicts=(verdict,))
    for stage in ('script','subtitles','captions','charts'):
        refs[stage] = synthetic_artifact(store,stage,uses=(use,))
    refs['asset_register'] = synthetic_artifact(store,'asset_register',licenses=(AssetLicense(asset_id='original-fixture',rights='owned'),))
    return ReviewManifest(story_id=story_id,revision=1,kind='synthetic_foundation',
        claim_revision_refs=(RevisionRef(record_id=claim.claim_id,revision=1,sha256=claim.record_hash()),),
        source_revision_refs=(RevisionRef(record_id=source.source_id,revision=1,sha256=source.record_hash()),),**refs)


def _negative_gates(state, snapshot, manifest):
    cases = {
        'missing_material_verdict':manifest.model_copy(update={'verification':synthetic_artifact(state.store,'verification')}),
        'missing_asset_rights':manifest.model_copy(update={'asset_register':synthetic_artifact(state.store,'asset_register',licenses=(AssetLicense(asset_id='unknown',rights='unknown'),))}),
        'stale_source_revision':manifest.model_copy(update={'source_revision_refs':()}),
        'unknown_schema':manifest.model_copy(update={'render':state.store.put(b'{"schema_id":"unknown","kind":"synthetic_foundation"}','application/json','1.0')}),
    }
    outcomes = {}
    for name, altered in cases.items():
        report = state.policy.evaluate_readiness(altered,state.clock())
        rejected = False
        try:
            state.transition(TransitionRequest(story_id=snapshot.story_id,target=StoryState.AWAITING_APPROVAL,
                expected_version=snapshot.version,caller_scope='demo-negative',idempotency_key=name,manifest=altered))
        except PolicyDenied:
            rejected = True
        outcomes[name] = {'rejected':rejected and not report.ready,
            'failed_checks':list(report.critical_failures),'state_unchanged':state.get(snapshot.story_id)==snapshot}
    for name, target in (('automatic_approval',StoryState.APPROVED_FOR_PUBLISH),('forbidden_publication',StoryState.PUBLISHED)):
        rejected = False
        try:
            state.transition(TransitionRequest(story_id=snapshot.story_id,target=target,
                expected_version=snapshot.version,caller_scope='demo-negative',idempotency_key=name))
        except PolicyDenied:
            rejected = True
        outcomes[name] = {'rejected':rejected,'state_unchanged':state.get(snapshot.story_id)==snapshot}
    return outcomes


def run_demo(output_dir: Path, telemetry_mode: Literal['offline','phoenix']) -> DemoReport:
    if telemetry_mode not in {'offline','phoenix'}:
        raise ValueError('invalid_telemetry_mode')
    root = output_dir.resolve()
    # Refuse to mix demonstrations or overwrite evidence from a prior run.
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if any(root.iterdir()):
        raise ValueError('demo_requires_empty_output_directory')
    runner = create_runner(root)
    try:
        store, policy = review_policy(root,runner.store.conn)
        runner.store = store
        state = StateService(store.conn,store,policy)
        request = synthetic_envelope(runner)
        output = runner.run(FakeStage(),request)
        if output.status != 'OK':
            raise ValueError('synthetic_execution_failed')
        candidate_ref = synthetic_artifact(store,'candidate',runner_output=output.output_ref)
        snapshot = state.create(request.story_id,request.run_id,candidate_ref,'demo','create')
        manifest = synthetic_manifest(store,snapshot.story_id)
        for target, stage in ((StoryState.CLUSTERED,'cluster'),(StoryState.SCORED,'score'),
                (StoryState.RESEARCHED,'dossier'),(StoryState.VERIFIED,'verification'),
                (StoryState.SCRIPTED,'script'),(StoryState.RENDERED,'render')):
            ref = (getattr(manifest,stage) if stage in MEMBERS else
                synthetic_artifact(store,stage,decision='ADVANCE',eligible=True))
            snapshot = state.transition(TransitionRequest(story_id=snapshot.story_id,target=target,
                expected_version=snapshot.version,caller_scope='demo',idempotency_key=stage,
                artifact=ref,trace_id=output.trace_id))
        negatives = _negative_gates(state,snapshot,manifest)
        snapshot = state.transition(TransitionRequest(story_id=snapshot.story_id,target=StoryState.AWAITING_APPROVAL,
            expected_version=snapshot.version,caller_scope='demo',idempotency_key='review',manifest=manifest,
            trace_id=output.trace_id))
        (root/'review-manifest.json').write_bytes(manifest.canonical_bytes())
        experiments = {}; pins = {}; outputs = [output]
        expected_evaluations = []
        adapter = PhoenixAdapter(root/'phoenix-registry.db')
        config = ExperimentConfig()
        for split in ('dev','holdout','prod_regression'):
            dataset = load_dataset(Path(config.dataset_root),split,'gate')
            bundle = run_experiment(config,dataset,runner,foundation_evaluators())
            expected_evaluations.extend((row.output.trace_id,row.output.span_id,evaluator_id)
                for row in bundle.records if row.output is not None for evaluator_id in bundle.evaluator_versions)
            outputs.extend(row.output for row in bundle.records if row.output is not None)
            path = persist_bundle(bundle,root/'evals')
            offline = adapter.export_offline(bundle,root/'offline'/bundle.record_hash())
            verified = load_offline(offline)
            summary = {**bundle.summary(),'bundle_path':str(path.relative_to(root)),
                'offline_path':str(offline.relative_to(root)),
                'offline_manifest_hash':digest((offline/'manifest.json').read_bytes()),
                'provenance':bundle.provenance,'evaluator_versions':bundle.evaluator_versions,
                'expected_rejections':sum(row.expected_outcome=='reject' and row.output is not None and row.output.status=='BLOCKED' for row in bundle.records),
                'offline_verified':verified.bundle==bundle}
            if telemetry_mode == 'phoenix':
                summary['phoenix'] = adapter.publish_experiment(bundle).model_dump(mode='json')
            experiments[split] = summary
            pins[split] = EvidencePin(path=str(offline),bundle_hash=bundle.record_hash())
        releases = ReleaseService(store.conn)
        candidate = ReleaseCandidate(evidence=pins)
        gate = releases.check(candidate)
        incomplete = releases.check(ReleaseCandidate(evidence={'dev':pins['dev']}))
        negatives['incomplete_release_evidence'] = {'rejected':incomplete.status in {'ERROR','BLOCKED'},'status':incomplete.status,
            'active_release_absent':releases.active('foundation') is None}
        # Snapshot before any ACK can drain pending rows; local evidence must
        # remain complete on both successful delivery and collector outage.
        pending_before = runner.telemetry.pending_records()
        traces = runner.telemetry.export_offline(root/'traces')
        trace_manifest = json.loads((traces/'manifest.json').read_bytes())
        outbox_preserved = runner.telemetry.pending_records()==pending_before
        local_health = runner.telemetry.health()
        # Verify persisted export bytes, not only its self-reported checksum.
        content = (traces/'spans.jsonl').read_bytes()
        exported = [SpanRecord.model_validate_json(line) for line in content.splitlines()]
        snapshot_verified = (exported==pending_before
            and trace_manifest['record_count']==len(exported)
            and trace_manifest['sha256']==digest(content)
            and content==b''.join(record.canonical_bytes()+b'\n' for record in pending_before))
        attempts = [AttemptRecord.model_validate_json(row[0]) for row in
            store.conn.execute('SELECT payload FROM execution_attempts')]
        expected = {(attempt.trace_id,attempt.span_id):str(attempt.invocation_id) for attempt in attempts}
        output_pairs = {(value.trace_id,value.span_id) for value in outputs}
        invocations = {(record.trace_id,record.span_id):record for record in exported if record.name=='agent.invoke'}
        missing = set(expected)-set(invocations)
        def linked_execution(value):
            invocation = invocations.get((value.trace_id,value.span_id))
            if invocation is None:
                return False
            root = any(record.name=='pipeline.run' and record.trace_id==value.trace_id
                and record.span_id==invocation.parent_span_id and record.parent_span_id is None for record in exported)
            model = value.status!='OK' or any(record.name=='model.call' and record.trace_id==value.trace_id
                and record.parent_span_id==value.span_id for record in exported)
            return root and model
        coverage_complete = (bool(expected) and len(attempts)==len(outputs)==len(output_pairs)
            and set(expected)==output_pairs and set(invocations)==set(expected)
            and all(invocations[pair].attributes.get('invocation.id')==identity for pair,identity in expected.items() if pair in invocations)
            and all(linked_execution(value) for value in outputs))
        evaluation_children = [(span.trace_id,span.parent_span_id,span.attributes.get('evaluator.id'))
                               for span in exported if span.name=='evaluator.run']
        evaluator_coverage = bool(expected_evaluations) and Counter(expected_evaluations)==Counter(evaluation_children)
        local_evidence = {'snapshot_verified':snapshot_verified,'execution_coverage_complete':coverage_complete,
            'evaluator_coverage_complete':evaluator_coverage,
            'expected_evaluator_span_count':len(expected_evaluations),'observed_evaluator_span_count':len(evaluation_children),
            'expected_execution_span_count':len(expected),'observed_execution_span_count':len(invocations),
            'missing_execution_span_count':len(missing),
            'complete':bool(snapshot_verified and coverage_complete and evaluator_coverage and outbox_preserved
                and local_health.pending_count==len(pending_before) and local_health.lost_records==0
                and local_health.persistence_failures==0
                and local_health.error_code in {None,'export_failed','export_timeout'})}
        export_report = None
        if telemetry_mode == 'phoenix':
            from app.observability.tracing import local_exporter
            runner.telemetry.exporter = local_exporter()
            export_report = runner.telemetry.flush(1).model_dump(mode='json')
        current = state.get(snapshot.story_id)
        passed = (local_evidence['complete'] and all(row['status']=='PASS' and row['critical_false_accepts']==0 and row['offline_verified'] for row in experiments.values())
            and all(row['rejected'] and row.get('state_unchanged',True) for row in negatives.values())
            and gate.status=='REQUIRES_BOOTSTRAP_APPROVAL' and current.state==StoryState.AWAITING_APPROVAL
            and current.approval_id is None and current.manifest_hash==manifest.record_hash())
        report = DemoReport(status='PASS' if passed else 'FAIL',final_state=current.state,
            story_id=current.story_id,run_id=current.run_id,manifest_hash=manifest.record_hash(),
            manifest_path='review-manifest.json',journal_path='state.db',
            member_paths={stage:str(store._path(getattr(manifest,stage)).relative_to(root)) for stage in MEMBERS},
            runner_output_path=str(store._path(output.output_ref).relative_to(root)),member_hashes={stage:getattr(manifest,stage).sha256 for stage in MEMBERS},
            runner_output_hash=output.output_ref.sha256,prompt_version=runner.prompt.prompt_version,prompt_hash=runner.prompt.sha256,
            experiments=experiments,negative_gates=negatives,release_gate=gate.model_dump(mode='json'),
            telemetry={'mode':telemetry_mode,'remote_visibility':'unverified','export':export_report,
                'offline_path':'traces','offline_manifest':trace_manifest,
                'health':runner.telemetry.health().model_dump(mode='json'),
                'local_evidence':local_evidence,
                'trace_ids':sorted({r.trace_id for r in pending_before}),
                'outbox_preserved':outbox_preserved})
        (root/'demo-report.json').write_bytes(report.canonical_bytes())
        # This is the ONLY CI upload: no paths, sessions, holdout rows or answers.
        public = {'schema_version':report.schema_version,'kind':report.kind,'status':report.status,
            'final_state':report.final_state,'manifest_hash':report.manifest_hash,
            'prompt_version':report.prompt_version,'prompt_hash':report.prompt_hash,
            'experiments':{split:{key:value for key,value in row.items() if key in {
                'split','status','bundle_hash','example_count','critical_false_accepts','observed_critical_false_accepts','expected_critical_outcomes','observed_critical_outcomes','expected_rejections','offline_verified'}}
                for split,row in experiments.items()},
            'negative_gates':{key:value['rejected'] for key,value in negatives.items()},
            'release_status':gate.status,'trace_export_hash':trace_manifest['sha256'],
            'limitations':report.limitations}
        (root/'public-report.json').write_bytes(canonical(public))
        return report
    finally:
        runner.telemetry.shutdown(0)
        runner.store.conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--offline',action='store_true')
    mode.add_argument('--phoenix',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('data/foundation-demo'))
    args = parser.parse_args(argv)
    try:
        report = run_demo(args.output,'offline' if args.offline else 'phoenix')
        print(json.dumps({'kind':report.kind,'status':report.status,'final_state':report.final_state,
            'report_path':str((args.output/'demo-report.json').resolve()),
            'public_report_path':str((args.output/'public-report.json').resolve())},sort_keys=True))
        return 0 if report.status=='PASS' else 1
    except Exception:
        print(json.dumps({'kind':'synthetic_foundation','status':'ERROR','error':'demo_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
