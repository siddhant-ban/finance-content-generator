"""Explicit Phoenix registrations for existing evidence; no tasks run remotely.

Dataset/experiment APIs are global in Phoenix. Project is routing metadata;
only trace resources create project-scoped spans. General projects never get
holdout executions. The complete local evidence is a restricted file, not wire data.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import shutil
import fcntl
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from phoenix.client import Client
from pydantic import Field

from app.config import Settings, validate_collector_endpoint
from app.contracts.common import Sha256, StrictModel
from app.evals.datasets import load_dataset
from app.evals.evaluators import REQUIRED
from app.evals.models import DatasetManifest, EvalExample, EvalResult, ExperimentBundle, LoadedDataset
from app.evals.run import HASH_PROVENANCE, aggregate, canonical, digest, persist_bundle, read_bundle, read_bundle_bytes, validate_bundle
from app.observability.sanitize import PROJECTS

RemoteID = Annotated[str, Field(pattern=r'^[A-Za-z0-9_+=:-]{1,256}$')]


class DatasetRegistration(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    dataset_hash: Sha256
    dataset_version: str
    split: Literal['dev', 'holdout', 'prod_regression']
    project: str
    status: Literal['registered', 'failed', 'pending']
    dataset_id: RemoteID | None = None
    dataset_version_id: RemoteID | None = None
    error_code: Literal['remote_unavailable', 'remote_state_unknown'] | None = None


class ExperimentRegistration(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    bundle_hash: Sha256
    dataset_hash: Sha256
    project: str
    status: Literal['registered', 'failed', 'pending']
    experiment_id: RemoteID | None = None
    detail: Literal['all_runs', 'aggregate_only']
    error_code: Literal['remote_unavailable', 'remote_state_unknown', 'observed_times_missing'] | None = None


class _SafeHTTP(httpx.Client):
    """Final prepared request boundary, after cookies/auth/default header merging."""
    acknowledged_mutations = 0
    def send(self, request, **kwargs):
        if (request.url.scheme,request.url.host,request.url.port) != self._origin:
            raise ValueError('phoenix_endpoint_denied')
        # No API keys, ambient headers, cookies, proxy auth, netrc or redirects.
        keep = {k:v for k,v in request.headers.items() if k.lower() in {
            'host','content-type','content-length','accept','accept-encoding','user-agent'}}
        request.headers.clear()
        request.headers.update(keep)
        kwargs['follow_redirects'] = False
        kwargs['auth'] = None
        response = super().send(request,**kwargs)
        if request.method not in {'GET','HEAD','OPTIONS'}:
            self.acknowledged_mutations += 1
        if response.is_redirect:
            raise ValueError('phoenix_redirect_denied')
        return response


def safe_client(endpoint: str | None = None, *, transport=None) -> Client:
    endpoint = validate_collector_endpoint(endpoint if endpoint is not None else Settings.from_env().phoenix_endpoint)
    url = urlsplit(endpoint)
    base = f'{url.scheme}://{url.netloc}/'
    http = _SafeHTTP(base_url=base,timeout=httpx.Timeout(1.0),trust_env=False,
                     follow_redirects=False,auth=None,transport=transport)
    parsed=httpx.URL(base)
    http._origin=(parsed.scheme,parsed.host,parsed.port)
    # This constructor branch bypasses .env.phoenix and PHOENIX_* resolution.
    return Client(http_client=http)


def opaque(dataset_hash: str, example_id: str) -> str:
    return digest(canonical([dataset_hash, example_id]))


def _context_id(value, length):
    return value if isinstance(value,str) and re.fullmatch('[0-9a-f]{'+str(length)+'}',value) and int(value,16) else None


def _dataset_payload(dataset: LoadedDataset) -> list[dict]:
    if dataset.kind != 'synthetic_foundation':
        raise ValueError('only_synthetic_foundation_supported')
    examples=tuple(dataset.iter_examples())
    if len(examples) != dataset.count or len({e.example_id for e in examples}) != dataset.count:
        raise ValueError('dataset_incomplete')
    rows=[]
    for example in examples:
        if example.rights.status != 'synthetic_fixture':
            raise ValueError('rights_not_cleared')
        key=opaque(dataset.dataset_hash,example.example_id)
        if dataset.split == 'holdout':
            rows.append({'input':{'opaque_id':key}, 'output':{}, 'metadata':{'opaque_id':key}})
            continue
        # Fixed schemas/enums/numbers only. Free text rights/adjudication, source
        # strings, artifact versions and local paths never cross this boundary.
        value=example.input
        reference=example.reference
        rows.append({'input':{'scenario':value.scenario,'value':value.data.value,
            'state':value.state,'metadata_present':value.metadata_present,
            'reference_count':len(value.reference_ids)},
            'output':{'outcome':reference.outcome,'rejection_code':reference.rejection_code,
                'value':None if reference.output is None else reference.output.value},
            'metadata':{'opaque_id':key,'target_behavior':example.labels.target_behavior,
                'critical_trap':example.labels.critical_trap,'severity':example.severity}})
    return rows


def _authoritative_dataset(dataset: LoadedDataset) -> LoadedDataset:
    """Copied wrappers/rows cannot substitute for their pinned loader source."""
    try:
        header=LoadedDataset.model_validate_json(dataset.model_dump_json())
        if dataset._source_root is None:
            raise ValueError()
        authoritative=load_dataset(dataset._source_root,header.split,header.purpose)
        rows=tuple(EvalExample.model_validate_json(row.model_dump_json()) for row in dataset.iter_examples())
        if header.summary() != authoritative.summary() or tuple(row.canonical_bytes() for row in rows) != tuple(row.canonical_bytes() for row in authoritative.iter_examples()):
            raise ValueError()
        return authoritative
    except Exception:
        raise ValueError('dataset_identity') from None


def _bind_bundle(bundle, dataset):
    examples={row.example_id:row for row in dataset.iter_examples()}
    if (bundle.split != dataset.split or bundle.provenance['dataset_hash'] != dataset.dataset_hash
            or bundle.provenance['dataset_version'] != dataset.dataset_version
            or bundle.expected_count != dataset.count
            or bundle.expected_critical_outcomes != bundle.config.repeats * sum(
                e.severity == 'critical' and e.reference.outcome == 'reject' for e in examples.values())):
        raise ValueError('bundle_dataset_identity')
    for row in bundle.records:
        example=examples.get(row.example_id)
        if (example is None or row.severity != example.severity
                or row.expected_outcome != example.reference.outcome):
            raise ValueError('bundle_dataset_identity')


def _public_result(result):
    # Metric is free text in restricted diagnostic bundles. Its public field
    # is derived from the fixed evaluator vocabulary, never copied from text.
    if result.evaluator_id not in REQUIRED:
        return None
    valid=result.metric == result.evaluator_id
    return {'evaluator_id':result.evaluator_id,'metric':result.evaluator_id,
        'status':result.status if valid else 'ERROR',
        'value':result.value if valid else None,
        'denominator':result.denominator if valid else None,
        'severity':result.severity,
        'reason':result.reason if valid else 'evaluator_error'}


def _dataset_for(bundle):
    dataset=load_dataset(Path(bundle.config.dataset_root),bundle.split,'gate')
    if dataset.dataset_hash != bundle.provenance['dataset_hash'] or dataset.dataset_version != bundle.provenance['dataset_version']:
        raise ValueError('dataset_pin_mismatch')
    return dataset


def _wire_payload(bundle: ExperimentBundle, dataset: LoadedDataset) -> dict:
    _bind_bundle(bundle,dataset)
    # Aggregate the same sanitized outcomes used for annotations, including for
    # holdout where only aggregate results may cross the privacy boundary.
    projected=tuple(row.model_copy(update={'results':tuple(
        EvalResult.model_validate(safe) for result in row.results
        if (safe := _public_result(result)) is not None)}) for row in bundle.records)
    rows=[]
    if bundle.split != 'holdout':
        for row in projected:
            output=row.output
            rows.append({'opaque_id':opaque(dataset.dataset_hash,row.example_id),'repeat':row.repeat,
                'output':{'status':output.status if output is not None else 'FAILED',
                    'value':output.output.value if output is not None and output.output is not None else None},
                'error':row.error,
                'started_at':None if row.started_at is None else row.started_at.isoformat(),
                'finished_at':None if row.finished_at is None else row.finished_at.isoformat(),
                'trace_id':None if output is None else _context_id(output.trace_id,32),
                'span_id':None if output is None else _context_id(output.span_id,16),
                'evaluations':[safe for result in row.results if (safe := _public_result(result)) is not None]})
    # Hashes bind full provenance without exporting strings/paths/provider secrets.
    return {'schema_version':'1.0','bundle_hash':bundle.record_hash(),
        'dataset_hash':dataset.dataset_hash,'dataset_version':dataset.dataset_version,
        'split':bundle.split,'status':bundle.status,'repetitions':bundle.config.repeats,
        'provenance':{k:v for k,v in bundle.provenance.items() if k in HASH_PROVENANCE and re.fullmatch(r'[0-9a-f]{64}',v)},
        'evaluator_versions':{k:digest(v.encode()) for k,v in bundle.evaluator_versions.items()},
        'aggregates':{k:v.model_dump(mode='json') for k,v in aggregate(projected).items()},
        'examples':_dataset_payload(dataset),'runs':rows}


def _remote_id(value):
    if not isinstance(value,str) or re.fullmatch(r'[A-Za-z0-9_+=:-]{1,256}',value) is None:
        raise ValueError('invalid_remote_id')
    return value


@dataclass(frozen=True, repr=False)
class OfflineEvidence:
    bundle: ExperimentBundle
    payload: dict
    manifest_bytes: bytes
    examples_bytes: bytes


def load_offline(directory: Path) -> OfflineEvidence:
    """Verify every artifact and its semantic binding before replay."""
    try:
        manifest=json.loads((directory/'manifest.json').read_bytes())
        core={k:v for k,v in manifest.items() if k != 'manifest_hash'}
        if set(core) != {'schema_version','bundle_hash','files','registration_status'} or manifest['manifest_hash'] != digest(canonical(core)):
            raise ValueError()
        expected={'payload.json',f'private/{manifest["bundle_hash"]}.json',
            'private/dataset-manifest.json','private/examples.jsonl'}
        if set(manifest['files']) != expected or manifest['schema_version'] != '1.0':
            raise ValueError()
        captured={}
        for name,sha in manifest['files'].items():
            path=directory/name
            if path.is_symlink():
                raise ValueError()
            captured[name]=path.read_bytes()
            if digest(captured[name])!=sha:
                raise ValueError()
        bundle=read_bundle_bytes(captured[f'private/{manifest["bundle_hash"]}.json'])
        if bundle.record_hash()!=manifest['bundle_hash']:
            raise ValueError()
        payload=json.loads(captured['payload.json'])
        header_bytes=captured['private/dataset-manifest.json']
        header=DatasetManifest.model_validate_json(header_bytes)
        content=captured['private/examples.jsonl']
        if header.record_hash() != bundle.provenance['dataset_hash'] or digest(content) != header.files[bundle.split].sha256:
            raise ValueError()
        examples=tuple(EvalExample.model_validate_json(line) for line in content.splitlines())
        if tuple(row.example_id for row in examples) != header.files[bundle.split].example_ids:
            raise ValueError()
        dataset=LoadedDataset(dataset_hash=header.record_hash(),dataset_version=header.dataset_version,
            agent_name=header.agent_name,kind=header.kind,split=bundle.split,purpose='gate',count=len(examples))
        dataset._examples=examples
        if payload != _wire_payload(bundle,dataset):
            raise ValueError()
        return OfflineEvidence(bundle,payload,header_bytes,content)
    except Exception:
        raise ValueError('offline_integrity') from None


class _RemoteUnknown(Exception):
    pass


class PhoenixAdapter:
    def __init__(self, registry: Path, *, client=None, endpoint=None, project='finance-media-orchestrator'):
        if project not in PROJECTS:
            raise ValueError('invalid_project')
        self.project=project
        self.endpoint=validate_collector_endpoint(endpoint if endpoint is not None else Settings.from_env().phoenix_endpoint)
        self.client=client if client is not None else safe_client(self.endpoint)
        registry.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(registry,os.O_CREAT | os.O_WRONLY,0o600); os.close(fd)
        self.registry=registry
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS phoenix_operations (key TEXT PRIMARY KEY, status TEXT NOT NULL, value TEXT)')

    @contextmanager
    def _db(self):
        connection=sqlite3.connect(self.registry,timeout=1)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _key(self, kind, identity):
        return digest(canonical([self.endpoint,self.project,kind,identity]))

    def _once(self,kind,identity,operation):
        key=self._key(kind,identity)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status,value FROM phoenix_operations WHERE key=?',(key,)).fetchone()
            if row:
                if row[0] == 'registered':
                    return json.loads(row[1])
                raise _RemoteUnknown()
            db.execute('INSERT INTO phoenix_operations VALUES (?,?,NULL)',(key,'pending'))
        http=getattr(self.client,'_client',None)
        mutations_before=getattr(http,'acknowledged_mutations',None)
        try:
            value=operation()
            data=canonical(value).decode()
            with self._db() as db:
                db.execute('UPDATE phoenix_operations SET status=?,value=? WHERE key=?',('registered',data,key))
            return value
        except (httpx.ConnectError,httpx.ConnectTimeout):
            # Dataset upload includes readback requests: a failed GET after
            # accepted POST is ambiguous. Only the first refused connection,
            # with no acknowledged mutation in this operation, is safe to retry.
            if mutations_before is not None and http.acknowledged_mutations == mutations_before:
                with self._db() as db:
                    db.execute('DELETE FROM phoenix_operations WHERE key=?',(key,))
            raise

    def _register(self,payload):
        identity=[payload['dataset_hash'],payload['dataset_version'],payload['split']]
        def upload():
            result=self.client.datasets.create_dataset(name='foundation-'+payload['split']+'-'+payload['dataset_hash'],
                examples=payload['examples'],timeout=1)
            mapping={}
            for example in result.examples:
                key=example['metadata']['opaque_id']
                if key in mapping:
                    raise ValueError('invalid_remote_mapping')
                mapping[key]=_remote_id(example['id'])
            if set(mapping) != {e['metadata']['opaque_id'] for e in payload['examples']}:
                raise ValueError('invalid_remote_mapping')
            return {'dataset_id':_remote_id(result.id),'dataset_version_id':_remote_id(result.version_id),'examples':mapping}
        return self._once('dataset',identity,upload)

    def register_dataset(self,dataset: LoadedDataset) -> DatasetRegistration:
        dataset=_authoritative_dataset(dataset)
        payload={'dataset_hash':dataset.dataset_hash,'dataset_version':dataset.dataset_version,
            'split':dataset.split,'examples':_dataset_payload(dataset)}
        base=dict(dataset_hash=dataset.dataset_hash,dataset_version=dataset.dataset_version,
                  split=dataset.split,project=self.project)
        try:
            result=self._register(payload)
            return DatasetRegistration(**base,status='registered',dataset_id=result['dataset_id'],dataset_version_id=result['dataset_version_id'])
        except Exception as error:
            return DatasetRegistration(**base,status='failed',error_code='remote_state_unknown' if isinstance(error,_RemoteUnknown) else 'remote_unavailable')

    def publish_experiment(self,bundle: ExperimentBundle) -> ExperimentRegistration:
        bundle=validate_bundle(bundle)
        offline=self.export_offline(bundle,self.registry.parent/'offline'/bundle.record_hash())
        evidence=load_offline(offline)
        return self._publish(evidence.bundle,evidence.payload)

    def _publish(self,bundle,payload):
        base=dict(bundle_hash=bundle.record_hash(),dataset_hash=bundle.provenance['dataset_hash'],
            project=self.project,detail='aggregate_only' if bundle.split == 'holdout' else 'all_runs')
        experiment_id=None
        if bundle.split != 'holdout' and any(row.started_at is None or row.finished_at is None for row in bundle.records):
            return ExperimentRegistration(**base,status='failed',error_code='observed_times_missing')
        try:
            registration=self._register(payload)
            experiment=self._once('experiment',bundle.record_hash(),lambda:{'id':_remote_id(self.client.experiments.create(
                dataset_id=registration['dataset_id'],dataset_version_id=registration['dataset_version_id'],
                experiment_name='foundation-'+bundle.record_hash(),
                experiment_metadata={'bundle_hash':bundle.record_hash(),'project':self.project,
                    'aggregates':payload['aggregates'],'provenance':payload['provenance'],
                    'privacy':base['detail'],'local_status':payload['status']}, repetitions=bundle.config.repeats,timeout=1)['id'])})
            experiment_id=experiment['id']
            for row,data in zip(bundle.records,payload['runs']):
                identity=[bundle.record_hash(),data['opaque_id'],row.repeat]
                output=row.output
                trace_id=data['trace_id']
                run=self._once('run',identity,lambda:{'id':_remote_id(self.client.experiments.log_run(
                    experiment_id=experiment_id,dataset_example_id=registration['examples'][data['opaque_id']],
                    output=data['output'],start_time=row.started_at,end_time=row.finished_at,
                    repetition_number=row.repeat+1,trace_id=trace_id,error=row.error,timeout=1)['id'])})
                for result in data['evaluations']:
                    self._once('evaluation',[*identity,result['evaluator_id']],lambda:{'id':_remote_id(self.client.experiments.log_evaluation(
                        experiment_run_id=run['id'],name=result['evaluator_id'],annotator_kind='CODE',
                        score=result['value'],label=result['status'],explanation=result['reason'],
                        error=result['reason'] if result['status'] == 'ERROR' else None,
                        metadata={'denominator':result['denominator'],'severity':result['severity'],
                            'evaluator_version_hash':payload['evaluator_versions'][result['evaluator_id']]},
                        trace_id=trace_id,start_time=row.started_at,end_time=row.finished_at,timeout=1)['id'])})
            return ExperimentRegistration(**base,status='registered',experiment_id=experiment_id)
        except Exception as error:
            return ExperimentRegistration(**base,status='failed',experiment_id=experiment_id,
                error_code='remote_state_unknown' if isinstance(error,_RemoteUnknown) else 'remote_unavailable')

    def export_offline(self,bundle: ExperimentBundle,directory: Path) -> Path:
        bundle=validate_bundle(bundle)
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or load_offline(directory).bundle.record_hash() != bundle.record_hash():
                raise ValueError('immutable_export_conflict')
            return directory
        directory.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        staged=Path(tempfile.mkdtemp(dir=directory.parent,prefix='.'+directory.name+'-pending-'))
        lock=directory.parent/('.'+directory.name+'-publish-lock')
        lock_fd=None
        try:
            self._write_offline(bundle,staged)
            # Read persisted bytes independently, including all pins/projections,
            # before publishing any directory that a reader can discover.
            load_offline(staged)
            # flock is released even on process death; retain the small lock
            # file so concurrent processes always lock the same inode.
            lock_fd=os.open(lock,os.O_CREAT | os.O_RDWR,0o600)
            fcntl.flock(lock_fd,fcntl.LOCK_EX)
            if directory.exists() or directory.is_symlink():
                if directory.is_symlink() or load_offline(directory).bundle.record_hash() != bundle.record_hash():
                    raise ValueError('immutable_export_conflict')
                return directory
            # All cooperating publishers serialize through the exclusive lock;
            # rename atomically exposes a complete independently verified tree.
            os.rename(staged,directory)
            return directory
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            shutil.rmtree(staged,ignore_errors=True)

    def _write_offline(self,bundle: ExperimentBundle,directory: Path) -> None:
        payload=_wire_payload(bundle,_dataset_for(bundle))
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        # persist_bundle atomically writes mode0600; enforce existing private dir too.
        private=directory/'private'; private.mkdir(exist_ok=True,mode=0o700); private.chmod(0o700)
        path=persist_bundle(bundle,private)
        path.chmod(0o600)
        root=Path(bundle.config.dataset_root)
        header=DatasetManifest.model_validate_json((root/'manifest.json').read_bytes())
        private_files={'private/dataset-manifest.json':(root/'manifest.json').read_bytes(),
            'private/examples.jsonl':(root/header.files[bundle.split].path).read_bytes()}
        for name,content in private_files.items():
            target=directory/name
            fd=os.open(target,os.O_WRONLY | os.O_CREAT | os.O_TRUNC,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(content)
        (directory/'payload.json').write_bytes(canonical(payload))
        core={'schema_version':'1.0','bundle_hash':bundle.record_hash(),
            'registration_status':'pending','files':{'payload.json':digest(canonical(payload)),
                f'private/{path.name}':digest(path.read_bytes()),
                **{name:digest(content) for name,content in private_files.items()}}}
        (directory/'manifest.json').write_bytes(canonical({**core,'manifest_hash':digest(canonical(core))}))

    def replay_offline(self,directory: Path) -> ExperimentRegistration:
        evidence=load_offline(directory)
        return self._publish(evidence.bundle,evidence.payload)
