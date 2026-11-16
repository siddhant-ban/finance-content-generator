"""Read-only local projections. Never expose source text or reviewer payloads.

Attempt envelopes are the usage source. Pending spans are navigation/current
signals only: acknowledged outbox rows disappear and are not history.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import math
from pathlib import Path
import re
import sqlite3
from typing import Literal
from uuid import UUID

from pydantic import Field
from app.contracts.common import StrictModel, UtcDatetime
from app.contracts.execution import AttemptRecord, OutputEnvelope, SyntheticData
from app.evals.run import read_bundle
from app.observability.models import SpanRecord

CRITICAL = frozenset(('unsupported_claim_reached_review', 'missing_asset_rights',
                      'forbidden_publishing_attempt', 'telemetry_failure'))

class LocalAlert(StrictModel):
    code: Literal['unsupported_claim_reached_review', 'missing_asset_rights',
                  'forbidden_publishing_attempt', 'telemetry_failure', 'critical_eval_failure']
    reference: str
    severity: Literal['critical'] = 'critical'
    delivery: Literal['local'] = 'local'
    scope: Literal['selected', 'bundle_coverage', 'telemetry_global'] = 'selected'

class TraceLink(StrictModel):
    trace_id: str
    span_id: str
    local_ref: str
    requested_projects: tuple[str, ...] = ()
    verified_owner: str | None = None
    remote_url: str | None = None

class RunIndex(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    run_id: UUID
    story_ids: tuple[UUID, ...] = ()
    traces: tuple[TraceLink, ...] = ()
    experiments: tuple[str, ...] = ()  # Immutable bundle hashes, never holdout rows.
    state_events: tuple[UUID, ...] = ()
    decision_count: int = 0
    missing_data: tuple[str, ...] = ()

class AgentSummary(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    attempt_count: int = 0
    request_count: int = 0
    successes: int = 0
    errors: int = 0
    retries: int = 0
    schema_failures: int = 0
    policy_failures: int = 0
    corrupt_attempts: int = 0
    sample_count: int = 0
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_samples: int = 0
    missing_usage_count: int = 0
    measured_cost: dict[str, Decimal] = Field(default_factory=dict)
    estimated_cost: dict[str, Decimal] = Field(default_factory=dict)
    measured_cost_samples: int = 0
    estimated_cost_samples: int = 0
    override_rate: float | None = None
    override_denominator: int = 0
    eval_bundle_count: int = 0
    eval_result_count: int = 0
    eval_unscopable_records: int = 0
    eval_failures: int | None = None
    critical_false_accepts: int | None = None
    degraded: bool = False
    alerts: tuple[LocalAlert, ...] = ()
    missing_data: tuple[str, ...] = ()


def percentile(values, fraction):
    """Nearest rank; empty windows have no percentile."""
    return sorted(values)[math.ceil(len(values)*fraction)-1] if values else None


def _link(trace, span, projects=()):
    if not (re.fullmatch('[0-9a-f]{32}',trace) and int(trace,16)
            and re.fullmatch('[0-9a-f]{16}',span) and int(span,16)):
        return None
    return TraceLink(trace_id=trace,span_id=span,local_ref=f'trace:{trace}/span:{span}',
                     requested_projects=tuple(sorted(projects)))


class MonitoringRepository:
    def __init__(self, database: Path, *, telemetry_path: Path | None = None,
                 eval_paths: tuple[Path, ...] = ()):
        self.database = database
        self.telemetry_path = telemetry_path
        self.eval_paths = eval_paths

    @staticmethod
    @contextmanager
    def connection(path):
        conn = sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True)
        try:
            conn.execute('PRAGMA query_only=ON')
            yield conn
        finally:
            conn.close()

    def _attempts(self):
        records, missing, corrupt = [], set(), 0
        try:
            with self.connection(self.database) as conn:
                for invocation,request,retry,status,payload in conn.execute(
                        'SELECT invocation_id,request_id,retry_count,status,payload FROM execution_attempts'):
                    try:
                        record = AttemptRecord.model_validate_json(payload)
                        if (str(record.invocation_id),str(record.request_id),record.retry_count,record.status) != (invocation,request,retry,status):
                            raise ValueError('journal_mismatch')
                        records.append(record)
                    except ValueError:
                        corrupt += 1
        except sqlite3.Error:
            missing.add('journal_unavailable')
        if corrupt:
            missing.add('corrupt_attempts_unattributed')
        return records, missing, corrupt

    def _telemetry(self):
        spans,missing,health = [], {'pending_spans_not_history'}, None
        if self.telemetry_path is None:
            return spans,missing | {'telemetry_unavailable'},health
        try:
            with self.connection(self.telemetry_path) as conn:
                for payload, in conn.execute('SELECT payload FROM telemetry_outbox ORDER BY id'):
                    try:
                        spans.append(SpanRecord.model_validate_json(payload))
                    except ValueError:
                        missing.add('corrupt_pending_spans')
                health=conn.execute('SELECT lost_records,persistence_failures,export_failures FROM telemetry_health WHERE singleton=1').fetchone()
                if health is None:
                    missing.add('telemetry_unavailable')
        except sqlite3.Error:
            missing.add('telemetry_unavailable')
        return spans,missing | {'telemetry_health_cumulative'},health

    def _evals(self):
        bundles,missing,seen = [],set(),set()
        for path in self.eval_paths:
            try:
                bundle=read_bundle(path)
                key=bundle.record_hash()
                if key not in seen:
                    bundles.append(bundle); seen.add(key)
            except (OSError,ValueError):
                missing.add('eval_evidence_invalid')
        if not bundles:
            missing.add('eval_evidence_unavailable')
        return bundles,missing

    @staticmethod
    def _output(record):
        # The only executable agent schema in foundations is SyntheticData.
        # Later slices must register their typed output decoder explicitly.
        if record.output_envelope is None or record.agent_name != 'foundation':
            return None
        output=OutputEnvelope[SyntheticData].model_validate_json(record.output_envelope)
        for field in ('run_id','story_id','invocation_id','agent_name','agent_version'):
            if getattr(output,field) != getattr(record,field):
                raise ValueError('envelope_identity')
        return output

    def versions(self, agent_name: str) -> tuple[str, ...]:
        from app.observability.sanitize import sanitize_attributes
        attempts, _, _ = self._attempts()
        return tuple(sorted({a.agent_version for a in attempts if a.agent_name == agent_name
            and sanitize_attributes({'agent.version': a.agent_version})}))

    def summary(self, agent_name: str, version: str | None, since: datetime,
                *, project: str | None = None) -> AgentSummary:
        since=UtcWindow(since=since).since
        attempts,missing,corrupt=self._attempts()
        spans,telemetry_missing,health=self._telemetry()
        missing |= telemetry_missing
        selected=[a for a in attempts if a.agent_name==agent_name
                  and (version is None or a.agent_version==version) and a.started_at>=since]
        if project is not None:
            keys={(s.trace_id,s.span_id) for s in spans if s.project==project}
            selected=[a for a in selected if (a.trace_id,a.span_id) in keys]
            missing.add('project_filter_pending_coverage_only')
        runs={a.run_id for a in selected}
        selected_invocations={a.invocation_id for a in selected}
        outputs,latencies,alerts=[],[],set()
        for attempt in selected:
            if attempt.finished_at is not None and attempt.finished_at>=attempt.started_at:
                latencies.append((attempt.finished_at-attempt.started_at).total_seconds()*1000)
            else:
                missing.add('latency_unavailable')
            try:
                output=self._output(attempt)
            except ValueError:
                output=None
                missing.add('invalid_output_envelope')
            outputs.append(output)
            if output is None:
                missing.add('output_evidence_unavailable')
                continue
            signals=set(output.warnings) | ({output.error.code} if output.error else set())
            for code in CRITICAL & signals:
                alerts.add((code,f'attempt:{attempt.invocation_id}'))
        for span in spans:
            if span.run_id in runs and span.start_time_ns>=int(since.timestamp()*1e9):
                if span.name=='publish.run' and span.attributes.get('error.code')=='policy_denied':
                    alerts.add(('forbidden_publishing_attempt',f'trace:{span.trace_id}/span:{span.span_id}'))
        if self.telemetry_path is not None and 'telemetry_unavailable' in missing:
            alerts.add(('telemetry_failure','telemetry:unavailable'))
        if 'corrupt_pending_spans' in missing:
            alerts.add(('telemetry_failure','telemetry:corrupt_pending_spans'))
        if health and any(health):
            alerts.add(('telemetry_failure','telemetry:health:cumulative'))
        usages=[o.usage for o in outputs if o is not None]
        def token_total(field):
            values=[getattr(u,field) for u in usages]
            return sum(values) if values and len(values)==len(selected) and all(v is not None for v in values) else None
        def costs(field):
            totals={}
            for usage in usages:
                money=getattr(usage,field)
                if money is not None:
                    totals[money.currency]=totals.get(money.currency,Decimal(0))+money.amount
            return totals
        bundles,eval_missing=self._evals()
        missing |= eval_missing
        evaluations=[]
        bundle_count=0
        unscopable=0
        coverage_alerts=set()
        for bundle in bundles:
            if bundle.config.agent_name != agent_name:
                continue
            missing_executions=max(0,bundle.expected_count*bundle.config.repeats-len(bundle.records))
            unknown=missing_executions+sum(r.output is None or r.started_at is None for r in bundle.records)
            if unknown:
                unscopable+=unknown
                missing.add('eval_scope_incomplete')
                coverage_alerts.add(bundle.record_hash())
            rows=[r for r in bundle.records if r.output is not None and r.output.agent_name==agent_name
                  and (version is None or r.output.agent_version==version)
                  and r.started_at is not None and r.started_at>=since
                  and (project is None or r.output.invocation_id in selected_invocations)]
            if bundle.status in {'ERROR','INCOMPLETE'}:
                if rows or unknown or not bundle.records:
                    missing.add('eval_results_incomplete')
                coverage_alerts.add(bundle.record_hash())
            if rows:
                evaluations.extend(rows); bundle_count+=1
                if any(row.expected_outcome=='reject' and row.severity=='critical' and row.output.status=='OK'
                        for row in rows) or any(result.severity=='critical' and result.status in {'FAIL','ERROR'}
                        for row in rows for result in row.results):
                    alerts.add(('critical_eval_failure',f'experiment:{bundle.record_hash()}'))
        eval_failures=sum(r.status in {'FAIL','ERROR'} for row in evaluations for r in row.results) if evaluations else None
        critical=sum(row.expected_outcome=='reject' and row.severity=='critical' and row.output.status=='OK' for row in evaluations) if evaluations else None
        if unscopable or 'eval_results_incomplete' in missing or 'eval_evidence_invalid' in missing:
            eval_failures=None
            critical=None
        if not evaluations:
            missing.add('eval_evidence_unavailable')
        missing |= {'override_unavailable_no_automatic_override', 'critical_signal_history_incomplete'}
        usage_samples=sum(u.input_tokens is not None and u.output_tokens is not None for u in usages)
        return AgentSummary(attempt_count=len(selected),request_count=len({a.request_id for a in selected}),
            successes=sum(a.status=='COMPLETED' for a in selected),errors=sum(a.status in {'FAILED','BLOCKED','INTERRUPTED'} for a in selected),
            retries=sum(a.retry_count>0 for a in selected),schema_failures=sum(o is not None and o.error is not None and o.error.code=='invalid_input' for o in outputs),
            policy_failures=sum(o is not None and o.error is not None and o.error.code=='policy_denied' for o in outputs),
            corrupt_attempts=corrupt,sample_count=len(latencies),p50_latency_ms=percentile(latencies,.5),p95_latency_ms=percentile(latencies,.95),
            input_tokens=token_total('input_tokens'),output_tokens=token_total('output_tokens'),usage_samples=usage_samples,missing_usage_count=len(selected)-usage_samples,
            measured_cost=costs('measured_cost'),estimated_cost=costs('estimated_cost'),
            measured_cost_samples=sum(u.measured_cost is not None for u in usages),estimated_cost_samples=sum(u.estimated_cost is not None for u in usages),
            eval_bundle_count=bundle_count,eval_result_count=sum(len(r.results) for r in evaluations),
            eval_unscopable_records=unscopable,eval_failures=eval_failures,critical_false_accepts=critical,
            degraded=bool({'journal_unavailable','telemetry_unavailable','corrupt_pending_spans','invalid_output_envelope'} & missing
                          or corrupt or (health and any(health)) or any(o.observability_status=='DEGRADED' for o in outputs if o)),
            alerts=tuple([LocalAlert(code=code,reference=ref,
                scope='telemetry_global' if ref.startswith('telemetry:') else 'selected') for code,ref in sorted(alerts)]
                + [LocalAlert(code='critical_eval_failure',reference=f'experiment:{key}',scope='bundle_coverage')
                   for key in sorted(coverage_alerts)]),missing_data=tuple(sorted(missing)))

    def run_index(self, run_id: UUID) -> RunIndex:
        run_id=UUID(str(run_id))
        attempts,missing,_=self._attempts()
        spans,telemetry_missing,_=self._telemetry()
        missing |= telemetry_missing
        selected=[a for a in attempts if a.run_id==run_id]
        stories={a.story_id for a in selected}
        keys={(a.trace_id,a.span_id) for a in selected}
        projects={}
        for span in spans:
            if span.run_id==run_id:
                keys.add((span.trace_id,span.span_id))
                if span.story_id: stories.add(span.story_id)
                projects.setdefault((span.trace_id,span.span_id),set()).add(span.project)
        traces=[]
        for key in sorted(keys):
            link=_link(*key,projects.get(key,()))
            if link: traces.append(link)
            else: missing.add('invalid_trace_reference')
        events,decisions=[],0
        try:
            with self.connection(self.database) as conn:
                stories.update(UUID(r[0]) for r in conn.execute('SELECT story_id FROM stories WHERE run_id=?',(str(run_id),)))
                for story in sorted(stories,key=str):
                    # Protected decision/event payloads are deliberately never read.
                    events.extend(UUID(r[0]) for r in conn.execute('SELECT event_id FROM state_events WHERE story_id=?',(str(story),)))
                    decisions+=conn.execute('SELECT count(*) FROM human_decisions WHERE story_id=?',(str(story),)).fetchone()[0]
        except (sqlite3.Error,ValueError):
            missing.add('state_index_unavailable')
        bundles,eval_missing=self._evals()
        missing |= eval_missing
        experiments=tuple(sorted(b.record_hash() for b in bundles if any(
            r.output is not None and r.output.run_id==run_id for r in b.records)))
        missing.add('phoenix_owner_unverified')
        return RunIndex(run_id=run_id,story_ids=tuple(sorted(stories,key=str)),traces=tuple(traces),
            experiments=experiments,state_events=tuple(sorted(events,key=str)),decision_count=decisions,
            missing_data=tuple(sorted(missing)))

class UtcWindow(StrictModel):
    since: UtcDatetime
