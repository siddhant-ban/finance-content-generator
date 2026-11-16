"""Real SDK contexts, disk outbox and injected offline exports."""
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
import pytest
from opentelemetry.sdk.trace.export import SpanExportResult
from app.observability.tracing import Telemetry
from app.observability.models import SpanRecord
from app.contracts.export import export_schemas

PROJECT = 'finance-media-orchestrator'
OTHER = 'finance-media-research'

class Exporter:
    def __init__(self, result=SpanExportResult.SUCCESS):
        self.result = result
        self.spans = []
    def export(self, spans):
        self.spans.extend(spans)
        return self.result
    def shutdown(self):
        pass

def create(tmp_path, exporter=None, **kwargs):
    return Telemetry(tmp_path / 'telemetry.sqlite', exporter=exporter or Exporter(), **kwargs)

def record(run_id=None):
    return SpanRecord(trace_id='a'*32, span_id=uuid4().hex[:16], project=PROJECT,
                      name='agent.invoke', start_time_ns=100, end_time_ns=200,
                      run_id=run_id, attributes={'model.provider': 'ollama'})

def test_context_across_projects_and_restart(tmp_path):
    exporter=Exporter(); t=create(tmp_path, exporter); run=uuid4(); story=uuid4()
    with t.story_context(run, story, None) as root:
        trace=root.get_span_context().trace_id
        parent=root.get_span_context().span_id
        with t.span('agent.invoke', OTHER, {'model.provider':'ollama'}) as child:
            assert child.get_span_context().trace_id == trace
    pending=t.pending_records()
    assert len(pending)==2
    assert pending[0].parent_span_id == f'{parent:016x}'
    assert pending[0].run_id==run
    assert pending[0].story_id==story
    assert pending[0].project==OTHER
    assert f'{trace:032x}' not in {run.hex, story.hex}
    t.shutdown(0)
    reopened=create(tmp_path, exporter)
    assert reopened.flush().exported==2
    assert {s.resource.attributes['openinference.project.name'] for s in exporter.spans}=={PROJECT,OTHER}
    assert exporter.spans[0].parent.span_id==parent

def test_w3c_remote_parent(tmp_path):
    t=create(tmp_path)
    parent='00-'+'1'*32+'-'+'2'*16+'-01'
    with t.story_context(uuid4(),uuid4(),parent) as span:
        assert span.get_span_context().trace_id==int('1'*32,16)
    assert t.pending_records()[0].parent_span_id=='2'*16
    with pytest.raises(ValueError):
        with t.story_context(uuid4(),uuid4(),'callback?token=SECRET'): pass

def test_failed_export_keeps_pending(tmp_path):
    exporter=Exporter(SpanExportResult.FAILURE); t=create(tmp_path,exporter)
    t.enqueue(record())
    report=t.flush()
    assert report.exported==0 and report.pending_count==1
    assert report.error_code=='export_failed'
    assert t.health().degraded
    exporter.result=SpanExportResult.SUCCESS
    assert t.flush().exported==1

def test_capacity_bytes_rows_atomic(tmp_path):
    path=tmp_path/'telemetry.sqlite'
    writers=[Telemetry(path,exporter=Exporter(),max_records=3) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda t:t.enqueue(record()),writers))
    assert sum(r.accepted for r in results)==3
    assert writers[0].health().pending_count==3
    assert writers[0].health().lost_records==3
    byte_path=tmp_path/'bytes'; byte_path.mkdir()
    probe=create(byte_path); item=record(); probe.enqueue(item)
    size=probe.health().pending_bytes
    capped=Telemetry(byte_path/'telemetry.sqlite',exporter=Exporter(),max_bytes=size)
    assert not capped.enqueue(record()).accepted
    assert capped.health().pending_count==1
    assert capped.health().pending_bytes==size

def test_overflow_preserves_queue_marks_run_degraded(tmp_path):
    run=uuid4(); t=create(tmp_path,max_records=1)
    first=record(run); assert t.enqueue(first).accepted
    result=t.enqueue(record(run))
    assert not result.accepted and result.error_code=='capacity_exceeded'
    assert t.pending_records()==[first]
    assert t.health().lost_records==1 and run in t.health().degraded_runs
    assert t.flush().exported==1
    assert t.health().degraded and t.health().lost_records==1
    reopened=create(tmp_path)
    assert reopened.health().lost_records==1 and run in reopened.health().degraded_runs

def test_disk_failure_surfaces(tmp_path):
    run=uuid4(); t=create(tmp_path)
    conn=sqlite3.connect(t.path)
    conn.execute("CREATE TRIGGER fail BEFORE INSERT ON telemetry_outbox BEGIN SELECT RAISE(ABORT, 'PASSWORD_SENTINEL'); END")
    conn.commit(); conn.close()
    result=t.enqueue(record(run))
    assert not result.accepted and result.error_code=='persistence_failed'
    health=t.health()
    assert health.degraded and health.persistence_failures==1
    assert health.lost_records==1 and run in health.degraded_runs
    assert 'PASSWORD_SENTINEL' not in health.model_dump_json()

def test_replay_preserves_ids(tmp_path):
    failed=Exporter(SpanExportResult.FAILURE); t=create(tmp_path,failed)
    t.enqueue(record()); first=t.pending_records()[0]
    t.flush(); t.shutdown(0)
    exported=Exporter(); reopened=create(tmp_path,exported)
    assert reopened.flush().exported==1
    span=exported.spans[0]
    assert f'{span.context.trace_id:032x}'==first.trace_id
    assert f'{span.context.span_id:016x}'==first.span_id
    assert span.start_time==first.start_time_ns and span.end_time==first.end_time_ns
    assert failed.spans[0].context==span.context

def test_flush_five_second_budget(tmp_path):
    release=threading.Event(); entered=threading.Event()
    class Blocked(Exporter):
        def export(self,spans):
            entered.set(); release.wait(10); return SpanExportResult.SUCCESS
        def shutdown(self): release.wait(10)
    t=create(tmp_path,Blocked()); t.enqueue(record())
    start=time.monotonic(); report=t.flush(timeout_seconds=0.08)
    assert time.monotonic()-start<0.5 and entered.is_set()
    assert report.error_code=='export_timeout' and report.pending_count is None
    assert t.health().pending_count == 1
    start=time.monotonic(); t.shutdown(timeout_seconds=0.08)
    assert time.monotonic()-start<0.5
    release.set()

def test_span_names_resources_events_and_exception_privacy(tmp_path,monkeypatch):
    monkeypatch.setenv('OTEL_RESOURCE_ATTRIBUTES','user.name=PrivateNameSentinel')
    monkeypatch.setenv('OTEL_SERVICE_NAME','TOKEN_SENTINEL')
    exporter=Exporter(); t=create(tmp_path,exporter)
    sentinels=['TOKEN_SENTINEL','COOKIE_SENTINEL','PASSWORD_SENTINEL','PrivateNameSentinel','ARTICLE_BODY_SENTINEL']
    with pytest.raises(RuntimeError):
        with t.story_context(uuid4(),uuid4(),None):
            with t.span('https://oauth/callback?code=TOKEN_SENTINEL',OTHER,{'model.name':'PrivateNameSentinel', 'article.body':'ARTICLE_BODY_SENTINEL'}) as span:
                span.add_event('COOKIE_SENTINEL',{'password':'PASSWORD_SENTINEL','retry.count':1})
                span.record_exception(RuntimeError('ARTICLE_BODY_SENTINEL'))
                raise RuntimeError('TOKEN_SENTINEL')
    raw=b''.join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert not any(s.encode() in raw for s in sentinels)
    assert t.flush().exported==2
    encoded=''.join(s.to_json() for s in exporter.spans)
    assert not any(s in encoded for s in sentinels)
    assert exporter.spans[0].name=='telemetry.operation'
    assert exporter.spans[0].status.status_code.name=='ERROR'

def test_own_database_does_not_commit_business_transaction(tmp_path):
    business=sqlite3.connect(tmp_path/'business.sqlite')
    business.execute('CREATE TABLE example (id INTEGER)'); business.commit()
    business.execute('INSERT INTO example VALUES (1)')
    exporter=Exporter(); t=create(tmp_path,exporter)
    with t.span('workflow.transition',PROJECT,{}): pass
    assert business.in_transaction and not exporter.spans
    business.rollback()
    assert business.execute('SELECT * FROM example').fetchall()==[]
    assert t.flush().exported==1
    business.close()

def test_telemetry_schemas_snapshot(tmp_path):
    assert 'telemetry.json' in export_schemas(tmp_path)
    assert json.loads((tmp_path/'telemetry.json').read_text())==json.loads(Path('schemas/v1/telemetry.json').read_text())
    assert record().schema_version=='1.0'

def test_endpoint_and_environment_headers_locked_down(tmp_path,monkeypatch):
    import requests
    sent = []
    def send(adapter, request, **kwargs):
        sent.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b''
        response.request = request
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS','authorization=TOKEN_SENTINEL')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT','https://elsewhere.invalid/secret')
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:3128')
    t=Telemetry(tmp_path/'telemetry.sqlite')
    t.enqueue(record())
    assert t.flush().exported == 1
    assert len(sent) == 1
    request, options = sent[0]
    assert request.url == 'http://127.0.0.1:6006/v1/traces'
    assert not any('TOKEN_SENTINEL' in str(v) for v in request.headers.values())
    assert not options.get('proxies')


def test_loopback_export_does_not_follow_redirects(tmp_path,monkeypatch):
    import requests
    sent=[]
    class Redirect(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            sent.append(request)
            response=requests.Response()
            response.status_code=302
            response.headers['Location']='https://external.invalid/token=TOKEN_SENTINEL'
            response.url=request.url
            response._content=b'ARTICLE_BODY_SENTINEL'
            response.request=request
            return response
        def close(self): pass
    t=Telemetry(tmp_path/'telemetry.sqlite')
    adapter = Redirect()
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send',
                        lambda self, request, **kwargs: adapter.send(request, **kwargs))
    t.enqueue(record())
    assert t.flush().error_code=='export_failed'
    assert len(sent)==1 and sent[0].url=='http://127.0.0.1:6006/v1/traces'
    assert t.health().pending_count==1

def test_offline_export_reloads_same_sanitized_spans(tmp_path):
    import hashlib
    t = create(tmp_path, Exporter(SpanExportResult.FAILURE))
    with t.story_context(uuid4(), uuid4()):
        with t.span('agent.invoke', OTHER, {'prompt.version': 'foundation.v1'}):
            pass
    original = t.pending_records()
    destination = t.export_offline(tmp_path/'offline')
    manifest = json.loads((destination/'manifest.json').read_text())
    content = (destination/'spans.jsonl').read_bytes()
    assert manifest['sha256'] == hashlib.sha256(content).hexdigest()
    restored = [SpanRecord.model_validate_json(line) for line in content.splitlines()]
    assert restored == original
    assert t.health().pending_count == 2
    replay = Telemetry(tmp_path/'replay.sqlite', exporter=Exporter())
    for span in restored:
        assert replay.enqueue(span).accepted
    assert replay.flush().exported == 2

def test_invalid_record_rejection_is_visible_in_run_health(tmp_path):
    t = create(tmp_path)
    run = uuid4()
    invalid = record(run).model_copy(update={'trace_id': '0' * 32})
    assert t.enqueue(invalid).error_code == 'invalid_record'
    assert t.health().lost_records == 1
    assert run in t.health().degraded_runs

def test_backward_clock_does_not_break_core_work(tmp_path):
    times = iter((100, 90))
    t = create(tmp_path, time_ns=lambda: next(times))
    with t.span('agent.invoke', PROJECT, {}):
        result = 42
    assert result == 42
    assert t.health().degraded
    assert t.health().lost_records == 1

def test_partial_collector_ack_does_not_drain_the_outbox(tmp_path, monkeypatch):
    import requests
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
    def send(adapter, request, **kwargs):
        reply = ExportTraceServiceResponse()
        reply.partial_success.rejected_spans = 1
        response = requests.Response()
        response.status_code = 200
        response._content = reply.SerializeToString()
        response.request = request
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    t = Telemetry(tmp_path/'telemetry.sqlite')
    t.enqueue(record())
    assert t.flush().error_code == 'export_failed'
    assert t.health().pending_count == 1

def test_explicit_loopback_endpoint_can_change_port(tmp_path, monkeypatch):
    import requests
    sent = []
    def send(adapter, request, **kwargs):
        sent.append(request.url)
        response = requests.Response()
        response.status_code = 200
        response._content = b''
        response.request = request
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    monkeypatch.setenv('FINANCE_PHOENIX_ENDPOINT', 'http://127.0.0.1:16006/v1/traces')
    t = Telemetry(tmp_path/'telemetry.sqlite')
    t.enqueue(record())
    assert t.flush().exported == 1
    assert sent == ['http://127.0.0.1:16006/v1/traces']

@pytest.mark.parametrize('status,available', [(200, True), (503, False), (302, False)])
def test_explicit_phoenix_health_probe(status, available, monkeypatch):
    import requests
    from app.observability.health import probe_phoenix
    sent = []
    def send(adapter, request, **kwargs):
        sent.append(request)
        response = requests.Response()
        response.status_code = status
        response.headers['Location'] = 'https://external.invalid/secret'
        response._content = b'ARTICLE_BODY_SENTINEL'
        response.request = request
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    report = probe_phoenix()
    assert report.available is available
    assert report.status_code == status
    assert len(sent) == 1 and sent[0].url == 'http://127.0.0.1:6006/healthz'
    assert 'ARTICLE_BODY_SENTINEL' not in report.model_dump_json()

def test_collector_cookies_are_neither_retained_nor_retransmitted(monkeypatch):
    from email.message import Message
    from types import SimpleNamespace
    import requests
    from app.observability.tracing import _LocalSession, ENDPOINT
    sent = []
    def send(adapter, request, **kwargs):
        sent.append(request)
        message = Message()
        message.add_header('Set-Cookie', 'collector_session=COOKIE_SENTINEL; Path=/')
        response = requests.Response()
        response.status_code = 200
        response._content = b''
        response.request = request
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    with _LocalSession(ENDPOINT) as session:
        session.post(ENDPOINT, data=b'')
        session.post(ENDPOINT, data=b'')
        assert all('Cookie' not in request.headers for request in sent)
        assert not list(session.cookies)

def test_timeout_does_not_perform_return_side_blocking_database_work(tmp_path):
    entered, locked, release = threading.Event(), threading.Event(), threading.Event()
    class Blocked(Exporter):
        def export(self, spans):
            entered.set()
            locked.wait(1)
            release.wait(2)
            return SpanExportResult.FAILURE
    t = create(tmp_path, Blocked())
    run = uuid4()
    t.enqueue(record(run))
    def lock_database():
        entered.wait(1)
        conn = sqlite3.connect(t.path)
        conn.execute('BEGIN IMMEDIATE')
        locked.set()
        release.wait(2)
        conn.rollback()
        conn.close()
    locker = threading.Thread(target=lock_database, daemon=True)
    locker.start()
    try:
        start = time.monotonic()
        result = t.flush(0.1)
        elapsed = time.monotonic() - start
        assert locked.is_set()
        assert result.error_code == 'export_timeout'
        assert elapsed < 0.115
    finally:
        release.set()
        locker.join(1)
        t.shutdown(0.2)
    assert run in t.health().degraded_runs

@pytest.mark.parametrize('field', ['start_time_ns', 'end_time_ns', 'event'])
def test_otlp_timestamp_overflow_cannot_poison_queue(tmp_path, field):
    from app.observability.models import SpanEvent
    t = create(tmp_path)
    run = uuid4()
    valid = record(run)
    if field == 'event':
        invalid = valid.model_copy(update={'events': (
            SpanEvent.model_construct(name='tool.completed', timestamp_ns=2**64, attributes={}),)})
    else:
        invalid = valid.model_copy(update={field: 2**64, 'end_time_ns': 2**64})
    result = t.enqueue(invalid)
    assert not result.accepted and result.error_code == 'invalid_record'
    assert t.enqueue(valid).accepted
    assert t.flush().exported == 1
    assert t.health().lost_records == 1
    assert run in t.health().degraded_runs
