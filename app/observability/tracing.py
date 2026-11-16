"""Manual SDK spans, sanitized durable handoff and bounded offline replay."""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import logging
import re
import sqlite3
import threading
import time
import hashlib
import json
import os
import tempfile
from uuid import UUID
from http.cookiejar import DefaultCookiePolicy
import requests
from pydantic import ValidationError
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter, Compression
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from google.protobuf.message import DecodeError
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor, ReadableSpan, Event, SpanLimits
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanContext, TraceFlags, TraceState, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from app.observability.models import SpanRecord, SpanEvent, EnqueueResult, ExportReport
from app.observability.outbox import Outbox
from app.observability.health import health_report
from app.observability.sanitize import sanitize_attributes, safe_operation, safe_project, safe_event
from app.config import Settings, validate_collector_endpoint

ENDPOINT = 'http://127.0.0.1:6006/v1/traces'
DB_ERRORS = (sqlite3.Error, OSError)

class _QuietExporterLog(logging.Filter):
    def filter(self, record):
        # The official exporter can log raw server bodies/exception messages.
        # Typed health codes are the only exporter error reporting boundary.
        return False

class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

class _LocalSession(requests.Session):
    def __init__(self, endpoint):
        super().__init__()
        self.endpoint = validate_collector_endpoint(endpoint)
        self.trust_env = False
        self.headers.clear()
        self.cookies.set_policy(_NoCookies())

    def send(self, request, **kwargs):
        if request.url != self.endpoint:
            raise ValueError('telemetry endpoint denied')
        # Requests adds cookies/auth during preparation, after request kwargs.
        request.headers.clear()
        request.headers['Content-Type'] = 'application/x-protobuf'
        request.prepare_content_length(request.body)
        kwargs['allow_redirects'] = False
        kwargs['proxies'] = {}
        return super().send(request, **kwargs)

    def request(self, method, url, **kwargs):
        if url != self.endpoint:
            raise ValueError('telemetry endpoint denied')
        kwargs['allow_redirects'] = False
        # SDK header resolution merges environment headers even when explicit
        # headers are supplied. Enforce the boundary at the final HTTP request.
        kwargs['headers'] = {'Content-Type': 'application/x-protobuf'}
        response = super().request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise requests.HTTPError('telemetry redirect denied')
        if 200 <= response.status_code < 300:
            try:
                acknowledgement = ExportTraceServiceResponse.FromString(response.content)
            except DecodeError as error:
                raise requests.HTTPError('invalid telemetry acknowledgement') from error
            if acknowledgement.partial_success.rejected_spans:
                # Keep the complete batch for same-ID retry. Accepted members may
                # be delivered twice; an explicit partial rejection is not success.
                raise requests.HTTPError('partial telemetry rejection')
        return response

def local_exporter(endpoint=None):
    endpoint = Settings.from_env().phoenix_endpoint if endpoint is None else validate_collector_endpoint(endpoint)
    logging.getLogger('opentelemetry.exporter.otlp.proto.http.trace_exporter').addFilter(_QuietExporterLog())
    return OTLPSpanExporter(endpoint=endpoint, headers={'Content-Type': 'application/x-protobuf'},
                            timeout=1.0, compression=Compression.NoCompression,
                            session=_LocalSession(endpoint))

def readable(record: SpanRecord) -> ReadableSpan:
    """Reconstruct the exact sanitized SDK identities and lifecycle for replay."""
    context = SpanContext(int(record.trace_id,16),int(record.span_id,16),False,
                          TraceFlags(record.trace_flags),TraceState())
    parent = None if record.parent_span_id is None else SpanContext(
        int(record.trace_id,16),int(record.parent_span_id,16),True,TraceFlags(record.trace_flags),TraceState())
    attributes = dict(record.attributes)
    if record.run_id is not None: attributes['run.id'] = str(record.run_id)
    if record.story_id is not None: attributes['story.id'] = str(record.story_id)
    return ReadableSpan(name=record.name, context=context, parent=parent,
        resource=Resource(record.resource), attributes=attributes,
        events=tuple(Event(event.name,attributes=event.attributes,timestamp=event.timestamp_ns) for event in record.events),
        status=Status(StatusCode[record.status]),start_time=record.start_time_ns,end_time=record.end_time_ns)

class _DurableProcessor(SpanProcessor):
    def __init__(self, telemetry, project):
        self.telemetry = telemetry
        self.project = project
    def on_start(self, span, parent_context=None):
        pass
    def on_end(self, span):
        attributes = sanitize_attributes(span.attributes or {})
        run = UUID(attributes['run.id']) if 'run.id' in attributes else None
        try:
            attributes['latency_ms'] = (span.end_time - span.start_time) / 1_000_000
            record = SpanRecord(trace_id=f'{span.context.trace_id:032x}',span_id=f'{span.context.span_id:016x}',
                parent_span_id=None if span.parent is None else f'{span.parent.span_id:016x}',
                trace_flags=int(span.context.trace_flags)&1,project=self.project,name=safe_operation(span.name),
                start_time_ns=span.start_time,end_time_ns=span.end_time,
                run_id=run,
                story_id=UUID(attributes['story.id']) if 'story.id' in attributes else None,
                attributes=attributes, status=span.status.status_code.name,
                events=tuple(SpanEvent(name=safe_event(event.name),timestamp_ns=event.timestamp,
                                      attributes=sanitize_attributes(event.attributes or {})) for event in span.events))
        except (ValidationError, ValueError, TypeError):
            self.telemetry._failure('invalid_record', (run,), lost=True)
            return
        result = self.telemetry.enqueue(record)
        receipt = self.telemetry._decision_enqueue.get()
        if receipt is not None and span.name == 'approval.decision' and result.accepted:
            receipt.append(record.trace_id)
    def shutdown(self):
        pass
    def force_flush(self, timeout_millis=30000):
        return True

class _SafeSpan:
    """Small manual API: free text never enters SDK names, events or errors."""
    def __init__(self, span, time_ns):
        self._span = span
        self._time_ns = time_ns
    def get_span_context(self):
        return self._span.get_span_context()
    def set_attribute(self, key, value):
        self._span.set_attributes(sanitize_attributes({key:value}))
    def set_attributes(self, values):
        self._span.set_attributes(sanitize_attributes(values))
    def update_name(self, name):
        self._span.update_name(safe_operation(name))
    def add_event(self, name, attributes=None):
        self._span.add_event(safe_event(name),sanitize_attributes(attributes or {}),timestamp=self._time_ns())
    def record_exception(self, exception):
        # Do not read str(exception), args, class name, traceback or stack.
        self.add_event('exception',{'error.code':'exception', 'error.type':'exception'})
        self._span.set_status(Status(StatusCode.ERROR))
    def set_status(self, status):
        code = status.status_code if isinstance(status,Status) else status
        if isinstance(code,StatusCode):
            self._span.set_status(Status(code))

class Telemetry:
    """Separate telemetry DB required; no business connection is accepted.

    Export happens only on explicit flush/shutdown, never in SDK callbacks.
    Export threads are daemons: even a permanently blocked injected exporter
    cannot extend the caller's deadline or create an atexit join.
    """
    def __init__(self, path: Path, *, exporter=None, endpoint=None, max_records=10_000,
                 max_bytes=52_428_800, clock=time.monotonic, time_ns=time.time_ns):
        self.path = path
        self.outbox = Outbox(path,max_records,max_bytes)
        self.exporter = exporter if exporter is not None else local_exporter(endpoint)
        self.clock = clock
        self.time_ns = time_ns
        self._lock = threading.RLock()
        self._providers = {}
        self._decision_enqueue = ContextVar('decision_enqueue',default=None)
        self._story = ContextVar('telemetry_story',default=None)
        self._fallback = dict(lost_records=0,persistence_failures=0,export_failures=0,
                              degraded_runs=set(),error_code=None)
        self._inflight = None
        self._closed = False
        try:
            self.outbox.initialize()
        except DB_ERRORS:
            self._failure('persistence_failed')

    def _failure(self, code, runs=(), lost=False):
        try:
            self.outbox.failure(code,runs,lost)
        except DB_ERRORS:
            self._memory_failure(code, runs, lost)

    def _memory_failure(self, code, runs=(), lost=False):
        with self._lock:
            field = 'persistence_failures' if code=='persistence_failed' else 'export_failures'
            self._fallback[field] += 1
            self._fallback['lost_records'] += int(lost)
            self._fallback['degraded_runs'].update(run for run in runs if run is not None)
            self._fallback['error_code'] = code

    def enqueue(self, record: SpanRecord) -> EnqueueResult:
        # Revalidate constructed/copied models; validation sanitizes every surface.
        try:
            record = SpanRecord.model_validate(record.model_dump())
        except (ValidationError, TypeError, AttributeError):
            run = record.run_id if isinstance(record, SpanRecord) and isinstance(record.run_id, UUID) else None
            self._failure('invalid_record', (run,), lost=True)
            return EnqueueResult(accepted=False,error_code='invalid_record')
        try:
            return self.outbox.enqueue(record)
        except DB_ERRORS:
            self._failure('persistence_failed',(record.run_id,),lost=True)
            return EnqueueResult(accepted=False,error_code='persistence_failed')

    def pending_records(self) -> list[SpanRecord]:
        try:
            return [record for _,record in self.outbox.pending()]
        except DB_ERRORS:
            self._failure('persistence_failed')
            return []

    def health(self):
        try:
            stats = self.outbox.stats()
        except DB_ERRORS:
            stats = dict(pending_count=None,pending_bytes=None,lost_records=0,persistence_failures=0,
                         export_failures=0,degraded_runs=(),error_code='persistence_failed')
            with self._lock:
                # Health reads must not count new loss on every polling attempt.
                self._fallback['persistence_failures'] = max(1,self._fallback['persistence_failures'])
        with self._lock:
            fallback = dict(self._fallback)
            fallback['degraded_runs'] = set(fallback['degraded_runs'])
        return health_report(stats,fallback)

    def export_offline(self, directory: Path) -> Path:
        """Export a checksummed, sanitized snapshot without draining the outbox."""
        # Do not convert a failed disk read into a misleading empty export.
        records = [record for _, record in self.outbox.pending()]
        content = b''.join(record.canonical_bytes() + b'\n' for record in records)
        manifest = dict(schema_version='1.0', format='telemetry.jsonl',
                        record_count=len(records), sha256=hashlib.sha256(content).hexdigest(),
                        status='offline_degraded')
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in (('spans.jsonl', content),
                           ('manifest.json', json.dumps(manifest, sort_keys=True).encode() + b'\n')):
            fd, temporary = tempfile.mkstemp(dir=directory, prefix='.pending-')
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, directory/name)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return directory

    def _tracer(self, project):
        # Preserve the requested resource project and W3C context. Phoenix21
        # stores each globally unique trace_id under one owning project, even
        # when its spans request different projects; do not rewrite identities
        # to manufacture cross-project storage. Task13 joins by run/trace IDs.
        with self._lock:
            if project not in self._providers:
                provider = TracerProvider(resource=Resource({'service.name':'finance-content-generator',
                    'openinference.project.name':project}),sampler=ALWAYS_ON,shutdown_on_exit=False,
                    span_limits=SpanLimits(max_attributes=64,max_events=64,max_event_attributes=32))
                provider.add_span_processor(_DurableProcessor(self,project))
                self._providers[project] = provider
            return self._providers[project].get_tracer('finance.observability','1.0')

    def decision_attempt(self, decision_id: UUID) -> str | None:
        """The receipt is authoritative. This event records the local decision attempt."""
        accepted = []
        token = self._decision_enqueue.set(accepted)
        story_token = self._story.set(None)
        try:
            with self.span('approval.decision','finance-media-orchestrator',{},_parent=Context()) as span:
                span.add_event('human_decision', {'decision_id':decision_id})
            return accepted[0] if accepted else None
        finally:
            self._story.reset(story_token)
            self._decision_enqueue.reset(token)

    @contextmanager
    def evaluator_span(self, output, evaluator_id):
        parent = TraceContextTextMapPropagator().extract({
            'traceparent':f'00-{output.trace_id}-{output.span_id}-01'})
        if not trace.get_current_span(parent).get_span_context().is_valid:
            raise ValueError('invalid_evaluator_parent')
        with self.span('evaluator.run','finance-media-orchestrator',{
                'evaluator.id':evaluator_id,'invocation.id':output.invocation_id,
                'run.id':output.run_id,'story.id':output.story_id},_parent=parent) as span:
            yield span

    @contextmanager
    def story_context(self, run_id: UUID, story_id: UUID, parent: str | None = None):
        if not isinstance(run_id,UUID) or not isinstance(story_id,UUID):
            raise TypeError('UUID business identifiers required')
        context = Context()
        if parent is not None:
            if not isinstance(parent,str) or not re.fullmatch(r'00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]',parent):
                raise ValueError('valid W3C traceparent required')
            context = TraceContextTextMapPropagator().extract({'traceparent':parent})
            if not trace.get_current_span(context).get_span_context().is_valid:
                raise ValueError('valid W3C traceparent required')
        token = self._story.set((run_id,story_id))
        try:
            with self.span('pipeline.run','finance-media-orchestrator',{},_parent=context) as span:
                yield span
        finally:
            self._story.reset(token)

    @contextmanager
    def span(self, name: str, project: str, attributes: Mapping[str,object], *, _parent=None):
        if self._closed:
            raise RuntimeError('telemetry is shut down')
        safe = sanitize_attributes(attributes)
        story = self._story.get()
        if story is not None:
            safe.update({'run.id':str(story[0]),'story.id':str(story[1])})
        raw = self._tracer(safe_project(project)).start_span(safe_operation(name),context=_parent,
                    attributes=safe,start_time=self.time_ns())
        facade = _SafeSpan(raw,self.time_ns)
        with trace.use_span(raw,end_on_exit=False,record_exception=False,set_status_on_exception=False):
            try:
                yield facade
            except BaseException as error:
                facade.record_exception(error)
                raise
            finally:
                raw.end(end_time=self.time_ns())

    def _export_worker(self, state, deadline):
        runs = ()
        try:
            while self.clock() < deadline:
                rows = self.outbox.pending(256)
                if not rows:
                    break
                runs = tuple(record.run_id for _,record in rows)
                state['runs'] = runs
                result = self.exporter.export([readable(record) for _,record in rows])
                if result != SpanExportResult.SUCCESS:
                    state['error'] = 'export_failed'
                    self._failure('export_failed',runs)
                    break
                self.outbox.acknowledge([key for key,_ in rows])
                state['exported'] += len(rows)
        except DB_ERRORS:
            state['error'] = 'persistence_failed'
            self._failure('persistence_failed',runs)
        except Exception:
            state['error'] = 'export_failed'
            self._failure('export_failed',runs)
        finally:
            state['done'].set()

    def flush(self, timeout_seconds: float = 5) -> ExportReport:
        if not isinstance(timeout_seconds,(int,float)) or not 0 <= timeout_seconds <= 5:
            raise ValueError('flush budget must be between zero and five seconds')
        deadline = self.clock() + timeout_seconds
        if timeout_seconds == 0:
            return ExportReport(pending_count=None)
        with self._lock:
            if self._inflight is None or self._inflight['done'].is_set():
                self._inflight = dict(done=threading.Event(),exported=0,error=None,runs=())
                thread = threading.Thread(target=self._export_worker,args=(self._inflight,deadline),daemon=True)
                thread.start()
            state = self._inflight
        completed = state['done'].wait(max(0,deadline-self.clock()))
        error = state['error']
        if not completed:
            error = 'export_timeout'
            # The budget is exhausted: even SQLite's short busy timeout would
            # exceed it. Preserve the queue and report process-local degradation.
            self._memory_failure(error,state['runs'])
            return ExportReport(exported=state['exported'],pending_count=None,error_code=error)
        if deadline-self.clock() < 0.05:
            return ExportReport(exported=state['exported'],pending_count=None,error_code=error)
        health = self.health()
        return ExportReport(exported=state['exported'],pending_count=health.pending_count,
                            error_code=error or health.error_code)

    def shutdown(self, timeout_seconds: float = 5):
        deadline = self.clock() + max(0,min(timeout_seconds,5))
        report = self.flush(max(0,deadline-self.clock())) if timeout_seconds > 0 else self.flush(0)
        self._closed = True
        for provider in self._providers.values():
            provider.shutdown()
        remaining = max(0,deadline-self.clock())
        if remaining:
            done = threading.Event()
            def close_exporter():
                try:
                    self.exporter.shutdown()
                except Exception:
                    pass
                finally:
                    done.set()
            threading.Thread(target=close_exporter,daemon=True).start()
            done.wait(remaining)
        return report
