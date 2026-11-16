"""Synthetic service acceptance; no external transport or authentication bypass."""
import json
import re
import sqlite3
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SpanExportResult

from app import demo
from app.auth.github import GitHubOAuth
from app.auth.sessions import SessionService
from app.config import Settings
from app.contracts.execution import SyntheticData
from app.contracts.workflow import ChangeKind, StoryState
from app.evals.phoenix import PhoenixAdapter, load_offline, safe_client
from app.evals.run import digest, read_bundle
from app.execution.fakes import FakeProvider, FakeStage
from app.execution.protocols import ModelResponse
from app.review.service import ReviewService
from app.web import Services, create_app
from app.workflow.state import PolicyDenied, StateService, TransitionRequest


@pytest.fixture(scope='module')
def accepted(tmp_path_factory):
    root = tmp_path_factory.mktemp('foundation-e2e')
    report = demo.run_demo(root, 'offline')
    return root, report


def browser(root):
    conn = sqlite3.connect(root/'state.db', check_same_thread=False)
    conn.execute('PRAGMA foreign_keys=ON')
    store, policy = demo.review_policy(root, conn)
    state = StateService(conn, store, policy)
    settings = Settings(github_client_id='fixture-client', github_client_secret='secret-e2e-sentinel',
                        github_allowed_ids=(123,), auth_loopback_development=True)
    def exchange(request):
        if request.url.host == 'github.com':
            return httpx.Response(200, json={'access_token':'token-e2e-sentinel','token_type':'bearer'})
        return httpx.Response(200, json={'id':123,'type':'User','login':'private-e2e-sentinel'})
    sessions = SessionService(conn, settings)
    review = ReviewService(state, current_session_verifier=sessions.verify_principal)
    app = create_app(settings, Services(GitHubOAuth(settings, transport=httpx.MockTransport(exchange)), sessions, review=review))
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    login = client.get('/auth/login', follow_redirects=False)
    oauth_state = parse_qs(urlsplit(login.headers['location']).query)['state'][0]
    callback = client.get('/auth/github/callback', params={'code':'code-e2e-sentinel','state':oauth_state}, follow_redirects=False)
    assert callback.status_code == 303
    assert client.get('/auth/session').status_code == 200
    return conn, state, review, client


def fields(response):
    assert response.status_code == 200
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text))


def test_full_fake_stage_eval_review_rework(accepted):
    root, report = accepted
    assert report.kind == 'synthetic_foundation'
    assert report.final_state == StoryState.AWAITING_APPROVAL
    assert set(report.experiments) == {'dev','holdout','prod_regression'}
    for split, summary in report.experiments.items():
        bundle = read_bundle(root/summary['bundle_path'])
        assert bundle.split == split and bundle.status == 'PASS'
        assert bundle.record_hash() == summary['bundle_hash']
        assert load_offline(root/summary['offline_path']).bundle == bundle
        assert summary['expected_rejections'] > 0 and bundle.critical_false_accepts == 0
    assert report.release_gate['status'] == 'REQUIRES_BOOTSTRAP_APPROVAL'
    assert all(value['rejected'] and value.get('state_unchanged',True) for value in report.negative_gates.values())
    conn, state, review, client = browser(root)
    try:
        snapshot = state.get(report.story_id)
        assert snapshot.manifest.record_hash() == report.manifest_hash
        assert len(report.member_hashes) == 9
        assert (root/report.journal_path).is_file()
        assert digest((root/report.runner_output_path).read_bytes()) == report.runner_output_hash
        for name, sha in report.member_hashes.items():
            assert getattr(snapshot.manifest,name).sha256 == sha
            assert digest((root/report.member_paths[name]).read_bytes()) == sha
        path = f'/review/{report.story_id}'
        payload = fields(client.get(path)) | {'decision':'approve','notes':''}
        headers = {'Origin':'http://127.0.0.1:8765'}
        assert client.post(path+'/decision', data=payload, headers=headers).status_code == 200
        approved = state.get(report.story_id)
        assert approved.state == StoryState.APPROVED_FOR_PUBLISH
        assert review.releases.active('foundation') is None  # Content != release approval.
        release_path = f'/releases/{report.release_gate["candidate_hash"]}'
        release_payload = fields(client.get(release_path)) | {'decision':'approve','bootstrap':'true'}
        assert client.post(release_path+'/decision',data=release_payload,headers=headers).status_code == 200
        assert review.releases.active('foundation').synthetic_only is True
        new_render = demo.synthetic_artifact(state.store,'render', label='reworked synthetic visual')
        revised_manifest = approved.manifest.model_copy(update={'revision':2,'render':new_render})
        revised = state.revise(report.story_id,revised_manifest,ChangeKind.VISUAL,approved.version,'e2e-rework')
        assert revised.state != StoryState.APPROVED_FOR_PUBLISH and revised.approval_id is None
        assert revised.manifest_hash != report.manifest_hash
        assert client.post(path+'/decision',data=payload,headers=headers).status_code == 200  # Historical receipt.
        assert state.get(report.story_id) == revised
        stale = fields(client.get(path)) | {'decision':'approve','notes':'','manifest_hash':report.manifest_hash}
        assert client.post(path+'/decision',data=stale,headers=headers).status_code == 409
    finally:
        client.close(); conn.close()


def test_phoenix_outage_no_silent_loss(tmp_path, accepted):
    root, report = accepted
    bundle = read_bundle(root/report.experiments['dev']['bundle_path'])
    def unavailable(request):
        raise httpx.ConnectError('private-e2e-sentinel',request=request)
    adapter = PhoenixAdapter(tmp_path/'registry.db',client=safe_client(transport=httpx.MockTransport(unavailable)))
    registration = adapter.publish_experiment(bundle)
    assert registration.status == 'failed' and registration.error_code == 'remote_unavailable'
    assert load_offline(tmp_path/'offline'/bundle.record_hash()).bundle == bundle
    runner = demo.create_runner(tmp_path)
    class Unavailable:
        def export(self, spans): return SpanExportResult.FAILURE
    runner.telemetry.exporter = Unavailable()
    try:
        result = runner.run(FakeStage(), demo.synthetic_envelope(runner))
        before = runner.telemetry.pending_records()
        assert result.status == 'OK'
        export = runner.telemetry.flush(1)
        assert export.error_code == 'export_failed'
        assert runner.telemetry.health().degraded
        destination = runner.telemetry.export_offline(tmp_path/'fallback')
        assert runner.telemetry.pending_records() == before
        records = [json.loads(line) for line in (destination/'spans.jsonl').read_text().splitlines()]
        assert {r['trace_id'] for r in records} == {r.trace_id for r in before}
        assert result.trace_id in {r['trace_id'] for r in records}
    finally:
        runner.telemetry.shutdown(0); runner.store.conn.close()


def test_model_timeout_partial_output_and_disk_fault(tmp_path, monkeypatch):
    runner = demo.create_runner(tmp_path)
    runner.sleeper = lambda _: None
    store, policy = demo.review_policy(tmp_path, runner.store.conn)
    runner.store = store
    state = StateService(store.conn, store, policy)
    envelope = demo.synthetic_envelope(runner)
    snapshot = state.create(envelope.story_id,envelope.run_id,demo.synthetic_artifact(runner.store,'candidate'),'e2e','create')
    calls = []
    runner.state_service = state
    runner.handoff = lambda output: calls.append(output)
    try:
        timeout = runner.run(FakeStage(FakeProvider(errors=[TimeoutError('secret-e2e-sentinel')]*3)),envelope)
        attempts = runner.attempts(envelope.invocation_id)
        assert timeout.status == 'FAILED' and timeout.error.code == 'unavailable'
        assert len(attempts) == 3 and len({a.invocation_id for a in attempts}) == 3
        partial = ModelResponse.model_construct(provider='fake',model='fake',parameters={},content={'value':'partial-article-body-sentinel'})
        with pytest.warns(UserWarning,match='Pydantic serializer warnings'):
            output = runner.run(FakeStage(FakeProvider(responses=[partial])),demo.synthetic_envelope(runner,story_id=snapshot.story_id))
        assert output.status == 'FAILED' and output.output_ref is None
        original_put = runner.store.put
        request = demo.synthetic_envelope(runner,story_id=snapshot.story_id)
        def disk_fault(*args,**kwargs): raise OSError('secret-e2e-sentinel')
        monkeypatch.setattr(runner.store,'put',disk_fault)
        with pytest.raises(OSError): runner.run(FakeStage(),request)
        monkeypatch.setattr(runner.store,'put',original_put)
        assert calls == [] and state.get(snapshot.story_id) == snapshot
        assert runner.recover_interrupted()[0].status == 'INTERRUPTED'
        with runner.telemetry.span('agent.invoke','finance-media-orchestrator',
                {'article.body':'partial-article-body-sentinel','authorization':'secret-e2e-sentinel'}):
            pass
        runner.telemetry.export_offline(tmp_path/'failure-traces')
        assert b'partial-article-body-sentinel' not in (tmp_path/'failure-traces/spans.jsonl').read_bytes()
        assert b'secret-e2e-sentinel' not in (tmp_path/'failure-traces/spans.jsonl').read_bytes()
    finally:
        runner.telemetry.shutdown(0); runner.store.conn.close()


def test_e2e_secrets_and_article_body_absent(accepted, caplog):
    root, report = accepted
    conn, state, review, client = browser(root)
    try:
        cookie = client.cookies.get('finance_session')
        raw = ('\n'.join(conn.iterdump())).encode()
        for sentinel in ('secret-e2e-sentinel','token-e2e-sentinel','code-e2e-sentinel','private-e2e-sentinel',cookie):
            assert sentinel.encode() not in raw
            assert sentinel not in caplog.text
        public = (root/'public-report.json').read_text()
        assert 'examples' not in public and 'records' not in public and 'finance_session' not in public
        for path in (root/'traces').rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                for sentinel in ('secret-e2e-sentinel','token-e2e-sentinel','private-e2e-sentinel','article-body-sentinel'):
                    assert sentinel.encode() not in data
    finally:
        client.close(); conn.close()


def test_no_auto_publish_even_after_approval(tmp_path, monkeypatch):
    report = demo.run_demo(tmp_path,'offline')
    conn, state, _, client = browser(tmp_path)
    publisher_calls = []
    def forbidden_connect(sock, address):
        publisher_calls.append(address)
        raise AssertionError("Outbound transport attempted during local approval")
    try:
        path = f'/review/{report.story_id}'
        payload = fields(client.get(path)) | {'decision':'approve','notes':''}
        # Authentication uses a mocked provider before observing local human actions.
        # The ASGI test client needs no TCP connection; any outbound connect is forbidden.
        monkeypatch.setattr(socket.socket,'connect',forbidden_connect)
        monkeypatch.setattr(socket.socket,'connect_ex',forbidden_connect)
        assert client.post(path+'/decision',data=payload,headers={'Origin':'http://127.0.0.1:8765'}).status_code == 200
        snapshot = state.get(report.story_id)
        with pytest.raises(PolicyDenied):
            state.transition(TransitionRequest(story_id=report.story_id,target=StoryState.PUBLISHED,
                expected_version=snapshot.version,caller_scope='e2e',idempotency_key='publish'))
        assert client.post(f'/publish/{report.story_id}').status_code == 404
        assert not any('publish' in getattr(route,'path','') for route in client.app.routes)
        assert publisher_calls == [] and snapshot.state != StoryState.PUBLISHED
        assert state.get(report.story_id) == snapshot
    finally:
        client.close(); conn.close()


def test_report_contract_links_local_journal_and_artifacts():
    assert {'journal_path','member_paths','runner_output_path'} <= set(demo.DemoReport.model_fields)


@pytest.mark.parametrize('delivery_ok',[True,False],ids=['ack','outage'])
def test_phoenix_ack_keeps_preflush_snapshot(tmp_path, monkeypatch, delivery_ok):
    from app.evals.phoenix import ExperimentRegistration
    from app.observability import tracing
    class Successful:
        def export(self, spans): return SpanExportResult.SUCCESS if delivery_ok else SpanExportResult.FAILURE
    monkeypatch.setattr(tracing,'local_exporter',lambda:Successful())
    def register(self, bundle):
        return ExperimentRegistration(bundle_hash=bundle.record_hash(),
            dataset_hash=bundle.provenance['dataset_hash'],project=self.project,
            status='registered' if delivery_ok else 'failed',experiment_id='fixture' if delivery_ok else None,
            error_code=None if delivery_ok else 'remote_unavailable',
            detail='aggregate_only' if bundle.split=='holdout' else 'all_runs')
    monkeypatch.setattr(PhoenixAdapter,'publish_experiment',register)
    report = demo.run_demo(tmp_path,'phoenix')
    assert report.status == 'PASS'
    assert report.telemetry['local_evidence']['complete'] is True
    if delivery_ok:
        assert report.telemetry['export']['pending_count'] == 0
    else:
        assert report.telemetry['export']['error_code'] == 'export_failed'
        assert report.telemetry['health']['degraded']
        assert report.telemetry['export']['pending_count'] == report.telemetry['offline_manifest']['record_count']
        assert all(row['phoenix']['status']=='failed' for row in report.experiments.values())
    assert report.telemetry['offline_manifest']['record_count'] > 0
    records = [json.loads(line) for line in (tmp_path/'traces/spans.jsonl').read_text().splitlines()]
    assert set(report.telemetry['trace_ids']) == {r['trace_id'] for r in records}
    assert report.telemetry['outbox_preserved']  # Export itself does not ACK.


@pytest.mark.parametrize(('record_loss','operation'),[(True,'agent.invoke'),(False,'agent.invoke'),(False,'evaluator.run')])
def test_demo_rejects_missing_execution_span(tmp_path, monkeypatch, record_loss, operation):
    from app.observability.models import EnqueueResult
    from app.observability.outbox import Outbox
    original = Outbox.enqueue
    dropped = []
    def lose_invocation(outbox, record):
        if record.name == operation and not dropped:
            dropped.append((record.trace_id,record.span_id))
            if record_loss:
                raise OSError('transient synthetic outbox failure')
            return EnqueueResult(accepted=True)  # Coverage must catch even absent loss metadata.
        return original(outbox,record)
    monkeypatch.setattr(Outbox,'enqueue',lose_invocation)
    report = demo.run_demo(tmp_path,'offline')
    assert len(dropped) == 1
    assert report.status == 'FAIL'
    assert report.final_state == StoryState.AWAITING_APPROVAL
    assert report.telemetry['health']['lost_records'] == int(record_loss)
    assert report.telemetry['local_evidence']['complete'] is False
    evidence=report.telemetry['local_evidence']
    assert evidence['missing_execution_span_count'] == int(operation=='agent.invoke')
    assert evidence['evaluator_coverage_complete'] == (operation!='evaluator.run')
    assert evidence['expected_evaluator_span_count']==54
    assert evidence['observed_evaluator_span_count']==54-int(operation=='evaluator.run')
    assert report.telemetry['local_evidence']['snapshot_verified'] is True
    assert json.loads((tmp_path/'public-report.json').read_bytes())['status'] == 'FAIL'
