"""Local monitoring fixtures are synthetic infrastructure evidence only."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
import json
import sqlite3

import pytest

from app.contracts.common import Money, Usage
from app.contracts.execution import AttemptRecord, ExecutionError
from app.execution.fakes import FakeStage
from app.evals.run import create_runner
from app.contracts.execution import InputEnvelope, SyntheticData
from app.monitoring.queries import MonitoringRepository
from app.observability.models import SpanRecord

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)

@pytest.fixture
def journal(tmp_path):
    runner = create_runner(tmp_path/'runtime')
    runner.clock = lambda: NOW
    data = SyntheticData(value=1)
    ref = runner.store.put(data.canonical_bytes(), 'application/json', '1.0')
    env = InputEnvelope[SyntheticData](run_id=uuid4(),story_id=uuid4(),invocation_id=uuid4(),
        agent_name='foundation',agent_version='1.0',prompt_id=runner.prompt.prompt_id,
        prompt_version=runner.prompt.prompt_version,prompt_hash=runner.prompt.sha256,
        config_version=runner.settings.config_version,config_hash=runner.settings.config_hash(),
        created_at=NOW,input=data,input_ref=ref)
    output = runner.run(FakeStage(),env)
    original = runner.attempts(env.invocation_id)[0]
    def add(ms, usage=Usage(), error=None, warnings=(), version='1.0', retry=0, finished=True):
        invocation, request = uuid4(), uuid4()
        result = output.model_copy(update={'invocation_id':invocation,'usage':usage,'warnings':warnings,
            'agent_version':version,'observability_status':'HEALTHY',
            **({'status':'FAILED','error':ExecutionError(code=error,retryable=False),'output':None,'output_ref':None} if error else {})})
        record = original.model_copy(update={'invocation_id':invocation,'request_id':request,
            'agent_version':version,'retry_count':retry,'started_at':NOW,
            'finished_at':NOW+timedelta(milliseconds=ms) if finished else None,
            'status':'FAILED' if error else 'COMPLETED','output_envelope':result.model_dump_json()})
        with runner.store.conn:
            runner.store.conn.execute('INSERT INTO execution_requests VALUES (?,?,?,?)',(str(request),'a'*64,env.model_dump_json(),runner.configuration.model_dump_json()))
            runner.store.conn.execute('INSERT INTO execution_attempts VALUES (?,?,?,?,?)',
                (str(invocation),str(request),retry,record.status,record.model_dump_json()))
        return record
    with runner.store.conn:
        runner.store.conn.execute('DELETE FROM execution_attempts')
    repo = MonitoringRepository(tmp_path/'runtime/state.db',telemetry_path=tmp_path/'runtime/telemetry.db')
    yield runner, env, add, repo
    runner.telemetry.shutdown(0)
    runner.store.conn.close()


def test_dashboard_latency_cost_and_missing_data(journal):
    runner,env,add,repo=journal
    add(10,Usage(input_tokens=2,output_tokens=3,measured_cost=Money(amount=Decimal('.1'),currency='USD'),estimated_cost=Money(amount=Decimal('.2'),currency='EUR')))
    add(20,Usage(input_tokens=4,output_tokens=5,measured_cost=Money(amount=Decimal('.3'),currency='SGD')))
    add(30,error='invalid_input',retry=1)
    add(40,error='policy_denied')
    add(999,version='2.0')
    summary=repo.summary('foundation','1.0',NOW)
    assert (summary.attempt_count,summary.request_count,summary.errors,summary.retries)==(4,4,2,1)
    assert (summary.schema_failures,summary.policy_failures)==(1,1)
    assert (summary.sample_count,summary.p50_latency_ms,summary.p95_latency_ms)==(4,20,40)
    assert summary.input_tokens is None and summary.output_tokens is None
    assert summary.usage_samples==2 and summary.missing_usage_count==2
    assert summary.measured_cost=={'USD':Decimal('.1'),'SGD':Decimal('.3')}
    assert summary.estimated_cost=={'EUR':Decimal('.2')}
    assert summary.measured_cost_samples==2 and summary.estimated_cost_samples==1
    assert summary.override_rate is None and summary.override_denominator==0
    assert summary.eval_failures is None and 'eval_evidence_unavailable' in summary.missing_data
    empty=repo.summary('foundation','1.0',NOW+timedelta(days=1))
    assert empty.sample_count==0 and empty.p95_latency_ms is None and empty.p50_latency_ms is None
    assert empty.input_tokens is None and empty.measured_cost=={}
    assert repo.summary('foundation',None,NOW).attempt_count==5
    with pytest.raises(ValueError): repo.summary('foundation',None,NOW.replace(tzinfo=None))


def test_run_index_links_projects_without_unified_tree(journal):
    runner,env,add,repo=journal
    record=add(10)
    index=repo.run_index(env.run_id)
    assert index.run_id==env.run_id and index.story_ids==(env.story_id,)
    trace=next(t for t in index.traces if t.span_id==record.span_id)
    assert trace.requested_projects==('finance-media-orchestrator',)
    assert trace.verified_owner is None and trace.remote_url is None
    assert trace.local_ref==f'trace:{record.trace_id}/span:{record.span_id}'
    assert index==repo.run_index(env.run_id)
    runner.telemetry.outbox.acknowledge([row[0] for row in runner.telemetry.outbox.pending()])
    after=repo.run_index(env.run_id)
    assert any(t.local_ref==trace.local_ref for t in after.traces)
    assert all(t.verified_owner is None for t in after.traces)
    assert 'pending_spans_not_history' in after.missing_data
    assert repo.run_index(uuid4()).story_ids==()


@pytest.mark.parametrize('code', ['unsupported_claim_reached_review','missing_asset_rights','forbidden_publishing_attempt'])
def test_critical_failures_create_local_alert(journal,code):
    runner,env,add,repo=journal
    add(10,warnings=(code,'PRIVATE source body reviewer notes callback-secret'))
    summary=repo.summary('foundation',None,NOW)
    assert [a.code for a in summary.alerts]==[code]
    assert summary.alerts[0].delivery=='local' and summary.alerts[0].severity=='critical'
    assert 'PRIVATE' not in summary.model_dump_json() and 'callback-secret' not in summary.model_dump_json()
    assert summary.errors==0  # Critical safety is distinct from ordinary error counts.
    runner.telemetry.outbox.failure('export_failed',(env.run_id,))
    assert 'telemetry_failure' in {a.code for a in repo.summary('foundation',None,NOW).alerts}


def test_corruption_missing_telemetry_and_readonly(journal,tmp_path):
    runner,env,add,repo=journal
    record=add(10)
    with runner.store.conn:
        runner.store.conn.execute('UPDATE execution_attempts SET payload=? WHERE invocation_id=?',('PRIVATE broken',str(record.invocation_id)))
    result=repo.summary('foundation',None,NOW)
    assert result.corrupt_attempts==1 and 'corrupt_attempts_unattributed' in result.missing_data
    assert 'PRIVATE' not in result.model_dump_json()
    unavailable=MonitoringRepository(tmp_path/'runtime/state.db',telemetry_path=tmp_path/'missing.db')
    assert 'telemetry_unavailable' in unavailable.summary('foundation',None,NOW).missing_data
    assert not (tmp_path/'missing.db').exists()
    missing=MonitoringRepository(tmp_path/'no.db')
    assert 'journal_unavailable' in missing.summary('foundation',None,NOW).missing_data
    assert not (tmp_path/'no.db').exists()
    with repo.connection(repo.database) as conn:
        with pytest.raises(sqlite3.OperationalError): conn.execute('DELETE FROM execution_attempts')


def test_pending_spans_never_double_count_usage_and_publish_alert(journal):
    runner,env,add,repo=journal
    record=add(10)
    span=SpanRecord(trace_id=record.trace_id,span_id='9'*16,project='finance-media-producer',
        name='publish.run',run_id=env.run_id,story_id=env.story_id,start_time_ns=int(NOW.timestamp()*1e9),
        end_time_ns=int(NOW.timestamp()*1e9)+100,attributes={'error.code':'policy_denied','usage.input_tokens':999})
    runner.telemetry.outbox.enqueue(span)
    result=repo.summary('foundation',None,NOW)
    assert result.input_tokens is None and result.attempt_count==1
    assert 'forbidden_publishing_attempt' in {a.code for a in result.alerts}
    trace=next(t for t in repo.run_index(env.run_id).traces if t.span_id=='9'*16)
    assert trace.requested_projects==('finance-media-producer',) and trace.verified_owner is None


def test_evaluation_aggregates_and_run_links_are_private(journal,tmp_path):
    from app.evals.datasets import load_dataset
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import run_experiment,persist_bundle
    runner,env,add,repo=journal
    bundle=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'holdout','gate'),runner,foundation_evaluators())
    path=persist_bundle(bundle,tmp_path/'evals')
    repo=MonitoringRepository(tmp_path/'runtime/state.db',eval_paths=(path,))
    summary=repo.summary('foundation','1.0',NOW-timedelta(days=1))
    assert summary.eval_failures==0 and summary.critical_false_accepts==0 and summary.eval_bundle_count==1
    index=repo.run_index(bundle.records[0].output.run_id)
    assert index.experiments==(bundle.record_hash(),)
    public=summary.model_dump_json()+index.model_dump_json()
    assert 'foundation-holdout' not in public and 'expected_outcome' not in public and str(path) not in public


def test_telemetry_outage_is_local_critical_alert(journal,tmp_path):
    runner,env,add,_=journal
    add(10)
    repo=MonitoringRepository(tmp_path/'runtime/state.db',telemetry_path=tmp_path/'unavailable.db')
    result=repo.summary('foundation',None,NOW)
    assert result.degraded
    assert 'telemetry_failure' in {a.code for a in result.alerts}


def test_project_filter_marks_partial_coverage(journal):
    runner,env,add,repo=journal
    add(10)
    filtered=repo.summary('foundation',None,NOW,project='finance-media-orchestrator')
    assert filtered.attempt_count==1 and 'project_filter_pending_coverage_only' in filtered.missing_data
    assert repo.summary('foundation',None,NOW,project='finance-media-producer').attempt_count==0


def test_dashboard_page_uses_readonly_projection(journal,monkeypatch):
    from streamlit.testing.v1 import AppTest
    runner,env,add,repo=journal
    add(10)
    monkeypatch.setenv('FINANCE_REVIEW_DATABASE',str(repo.database))
    monkeypatch.setenv('FINANCE_TELEMETRY_DATABASE',str(repo.telemetry_path))
    page=AppTest.from_file(str(Path('app/monitoring/ui.py').resolve())).run()
    assert not page.exception
    assert {s.label for s in page.selectbox}=={'Agent','Agent version','Requested Phoenix project'}
    assert any('unknown' in c.value.lower() for c in page.caption)
    page.text_input[0].set_value(str(env.run_id)).run()
    assert not page.exception
    assert 'finance_session' not in repr(page)


def test_run_index_exposes_only_opaque_protected_history(journal):
    runner,env,add,repo=journal
    add(10)
    event,decision=uuid4(),uuid4()
    with runner.store.conn:
        runner.store.conn.execute('INSERT INTO stories(story_id,run_id,state,version,payload) VALUES (?,?,?,?,?)',
            (str(env.story_id),str(env.run_id),'DISCOVERED',1,'{}'))
        runner.store.conn.execute('INSERT INTO state_events VALUES (?,?,?,?,?)',
            (str(event),str(env.story_id),1,'PRIVATE reviewer reason',json.dumps({'actor_id':'PRIVATE identity','notes':'PRIVATE body'})))
        runner.store.conn.execute('INSERT INTO human_decisions VALUES (?,?,?)',
            (str(decision),str(env.story_id),json.dumps({'reviewer_id':'PRIVATE identity','notes':'PRIVATE notes'})))
    result=repo.run_index(env.run_id)
    assert result.state_events==(event,) and result.decision_count==1
    assert 'PRIVATE' not in result.model_dump_json() and str(decision) not in result.model_dump_json()
    assert repo.summary('foundation',None,NOW).override_rate is None


def test_failed_critical_evaluation_is_aggregate_local_alert(journal,tmp_path):
    from app.evals.datasets import load_dataset
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import run_experiment,persist_bundle,aggregate,assess
    runner,env,add,repo=journal
    bundle=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'),runner,foundation_evaluators())
    first=bundle.records[0]
    failed=first.results[0].model_copy(update={'status':'FAIL','value':0.0,'severity':'critical'})
    records=(first.model_copy(update={'results':(failed,*first.results[1:])}),*bundle.records[1:])
    status,critical,issues=assess(records,bundle.expected_count,1,())
    bundle=bundle.model_copy(update={'records':records,'aggregates':aggregate(records),'status':status,'critical_false_accepts':critical,'issues':issues})
    path=persist_bundle(bundle,tmp_path/'evals')
    repo=MonitoringRepository(repo.database,eval_paths=(path,path))
    result=repo.summary('foundation',None,NOW-timedelta(days=1))
    assert result.eval_bundle_count==1 and result.eval_failures==1
    assert 'critical_eval_failure' in {a.code for a in result.alerts}
    assert first.example_id not in result.model_dump_json()
    broken=tmp_path/'broken.json'; broken.write_text('{PRIVATE}')
    bad=MonitoringRepository(repo.database,eval_paths=(broken,)).summary('foundation',None,NOW)
    assert bad.eval_failures is None and 'eval_evidence_invalid' in bad.missing_data
    assert 'PRIVATE' not in bad.model_dump_json()


def test_unscopable_eval_execution_error_remains_unknown(journal,tmp_path):
    from app.evals.datasets import load_dataset
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import run_experiment,persist_bundle,aggregate,assess,aggregate_usage
    runner,env,add,repo=journal
    bundle=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'),runner,foundation_evaluators())
    first=bundle.records[0].model_copy(update={'output':None,'results':(),'error':'execution_error','started_at':None,'finished_at':None})
    records=(first,*bundle.records[1:])
    status,critical,issues=assess(records,bundle.expected_count,1,())
    bundle=bundle.model_copy(update={'records':records,'aggregates':aggregate(records),'status':status,'critical_false_accepts':critical,'issues':issues,'usage':aggregate_usage(records)})
    path=persist_bundle(bundle,tmp_path/'evals')
    result=MonitoringRepository(repo.database,eval_paths=(path,)).summary('foundation',None,NOW-timedelta(days=1))
    assert result.eval_failures is None and result.critical_false_accepts is None
    assert 'eval_scope_incomplete' in result.missing_data
    assert result.eval_unscopable_records==1 and result.eval_result_count==12
    assert 'critical_eval_failure' in {a.code for a in result.alerts}


@pytest.mark.parametrize('excluded_by', ['version','time','project','same_run_project'])
def test_eval_alert_false_accept_respects_selected_scope(journal,tmp_path,excluded_by):
    from app.evals.datasets import load_dataset
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import run_experiment,persist_bundle,aggregate,assess,aggregate_usage
    runner,env,add,repo=journal
    bundle=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'),runner,foundation_evaluators())
    clean=next(row for row in bundle.records if row.expected_outcome=='accept')
    rejected=next(row for row in bundle.records if row.expected_outcome=='reject')
    accepted=clean.output.model_copy(update={'run_id':clean.output.run_id if excluded_by=='same_run_project' else rejected.output.run_id,
        'story_id':rejected.output.story_id,'invocation_id':rejected.output.invocation_id,
        'trace_id':rejected.output.trace_id,'span_id':rejected.output.span_id,
        'agent_version':'2.0' if excluded_by=='version' else clean.output.agent_version})
    updates={'output':accepted,'severity':'critical'}
    if excluded_by=='time':
        updates.update(started_at=NOW-timedelta(days=2),finished_at=NOW-timedelta(days=2)+timedelta(seconds=1))
    false_accept=rejected.model_copy(update=updates)
    rows=(clean,false_accept)
    status,critical,issues=assess(rows,len(rows),1,())
    bundle=bundle.model_copy(update={'records':rows,'example_count':len(rows),'expected_count':len(rows),'expected_critical_outcomes':1,
        'aggregates':aggregate(rows),'status':status,'critical_false_accepts':critical,'issues':issues,'usage':aggregate_usage(rows)})
    path=persist_bundle(bundle,tmp_path/'evals')
    project=None
    if excluded_by in {'project','same_run_project'}:
        if excluded_by=='same_run_project':
            with runner.store.conn:
                payload=runner.store.conn.execute('SELECT payload FROM execution_attempts WHERE invocation_id=?',
                    (str(rejected.output.invocation_id),)).fetchone()[0]
                attempt=AttemptRecord.model_validate_json(payload)
                output=json.loads(attempt.output_envelope)
                output['run_id']=str(clean.output.run_id)
                attempt=attempt.model_copy(update={'run_id':clean.output.run_id,'output_envelope':json.dumps(output)})
                runner.store.conn.execute('UPDATE execution_attempts SET payload=? WHERE invocation_id=?',
                    (attempt.model_dump_json(),str(attempt.invocation_id)))
        project='finance-media-producer'
        # Route only the clean invocation's pending span to the requested project.
        with runner.telemetry.outbox.connection() as conn:
            with conn:
                for identifier,payload in conn.execute('SELECT id,payload FROM telemetry_outbox'):
                    span=SpanRecord.model_validate_json(payload)
                    if excluded_by=='same_run_project' and span.trace_id==rejected.output.trace_id:
                        span=span.model_copy(update={'run_id':clean.output.run_id})
                        payload=span.model_dump_json()
                        conn.execute('UPDATE telemetry_outbox SET payload=?,size_bytes=? WHERE id=?',
                            (payload,len(payload.encode()),identifier))
                    if span.span_id==clean.output.span_id and span.trace_id==clean.output.trace_id:
                        payload=span.model_copy(update={'project':project}).model_dump_json()
                        conn.execute('UPDATE telemetry_outbox SET payload=?,size_bytes=? WHERE id=?',
                            (payload,len(payload.encode()),identifier))
    repo=MonitoringRepository(repo.database,telemetry_path=repo.telemetry_path,eval_paths=(path,))
    result=repo.summary('foundation','1.0',NOW-timedelta(days=1),project=project)
    assert result.critical_false_accepts==0 and result.eval_failures==0
    if excluded_by in {'project','same_run_project'}:
        assert result.attempt_count==1 and result.eval_result_count==6
    assert not any(a.code=='critical_eval_failure' for a in result.alerts)
    selected_all=repo.summary('foundation',None,NOW-timedelta(days=3))
    assert selected_all.critical_false_accepts==1
    assert any(a.code=='critical_eval_failure' and a.scope=='selected' for a in selected_all.alerts)


def test_whole_bundle_eval_error_alert_has_explicit_coverage_scope(journal,tmp_path):
    from app.evals.datasets import load_dataset
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import run_experiment,persist_bundle,aggregate,assess
    runner,env,add,repo=journal
    bundle=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'),runner,foundation_evaluators())
    first=bundle.records[0]
    errored=first.results[0].model_copy(update={'status':'ERROR','value':None,'reason':'evaluator_error'})
    rows=(first.model_copy(update={'output':first.output.model_copy(update={'agent_version':'2.0'}),
        'results':(errored,*first.results[1:])}),*bundle.records[1:])
    status,critical,issues=assess(rows,bundle.expected_count,1,())
    bundle=bundle.model_copy(update={'records':rows,'aggregates':aggregate(rows),'status':status,
        'critical_false_accepts':critical,'issues':issues})
    path=persist_bundle(bundle,tmp_path/'evals')
    result=MonitoringRepository(repo.database,eval_paths=(path,)).summary('foundation','1.0',NOW-timedelta(days=1))
    alert=next(a for a in result.alerts if a.code=='critical_eval_failure')
    assert alert.scope=='bundle_coverage'
    assert not any(a.code=='critical_eval_failure' and a.scope=='selected' for a in result.alerts)
    assert result.eval_failures is None and 'eval_results_incomplete' in result.missing_data


def test_empty_incomplete_bundle_keeps_mixed_eval_coverage_unknown(journal,tmp_path):
    from app.evals.datasets import load_dataset
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import run_experiment,persist_bundle,aggregate,assess,aggregate_usage
    runner,env,add,repo=journal
    clean=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'),runner,foundation_evaluators())
    clean_path=persist_bundle(clean,tmp_path/'evals')
    status,critical,issues=assess((),clean.expected_count,clean.config.repeats,())
    empty=clean.model_copy(update={'records':(),'example_count':0,'aggregates':aggregate(()),
        'status':status,'critical_false_accepts':critical,'issues':issues,'usage':aggregate_usage(())})
    empty_path=persist_bundle(empty,tmp_path/'evals')
    repo=MonitoringRepository(repo.database,eval_paths=(clean_path,empty_path))
    result=repo.summary('foundation','1.0',min(r.started_at for r in clean.records))
    assert result.eval_result_count==18
    assert result.eval_failures is None and result.critical_false_accepts is None
    assert result.eval_unscopable_records==clean.expected_count
    assert 'eval_scope_incomplete' in result.missing_data
    assert any(a.reference==f'experiment:{empty.record_hash()}' and a.scope=='bundle_coverage' for a in result.alerts)


@pytest.mark.parametrize('excluded_by',['version','time'])
def test_fully_excluded_error_bundle_preserves_clean_selected_totals(journal,tmp_path,excluded_by):
    from app.evals.datasets import load_dataset
    from app.evals.models import ExperimentConfig
    from app.evals.evaluators import foundation_evaluators
    from app.evals.run import run_experiment,persist_bundle,aggregate,assess
    runner,env,add,repo=journal
    original=run_experiment(ExperimentConfig(),load_dataset(Path('evals/foundation/1.0.0'),'dev','gate'),runner,foundation_evaluators())
    since=min(row.started_at for row in original.records)
    selected_version='2.0' if excluded_by=='version' else '1.0'
    clean_rows=tuple(row.model_copy(update={'output':row.output.model_copy(update={'agent_version':selected_version})})
        for row in original.records)
    clean=original.model_copy(update={'records':clean_rows})
    clean_path=persist_bundle(clean,tmp_path/'evals')
    error_rows=original.records
    if excluded_by=='time':
        error_rows=tuple(row.model_copy(update={'started_at':row.started_at-timedelta(days=2),
            'finished_at':row.finished_at-timedelta(days=2)}) for row in error_rows)
    first=error_rows[0]
    failed=first.results[0].model_copy(update={'status':'ERROR','value':None,'reason':'evaluator_error'})
    error_rows=(first.model_copy(update={'results':(failed,*first.results[1:])}),*error_rows[1:])
    status,critical,issues=assess(error_rows,original.expected_count,original.config.repeats,())
    errored=original.model_copy(update={'records':error_rows,'aggregates':aggregate(error_rows),
        'status':status,'critical_false_accepts':critical,'issues':issues})
    error_path=persist_bundle(errored,tmp_path/'evals')
    result=MonitoringRepository(repo.database,eval_paths=(clean_path,error_path)).summary('foundation',selected_version,since)
    assert result.eval_result_count==18 and result.eval_bundle_count==1
    assert result.eval_failures==0 and result.critical_false_accepts==0
    assert 'eval_results_incomplete' not in result.missing_data
    assert any(a.reference==f'experiment:{errored.record_hash()}' and a.scope=='bundle_coverage' for a in result.alerts)
    assert not any(a.code=='critical_eval_failure' and a.scope=='selected' for a in result.alerts)
