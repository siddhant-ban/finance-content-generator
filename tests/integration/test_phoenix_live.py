"""Explicit local service compatibility probe; never starts infrastructure."""
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import pytest
import httpx

from app.evals.datasets import load_dataset
from app.evals.evaluators import foundation_evaluators
from app.evals.models import ExperimentConfig
from app.evals.phoenix import PhoenixAdapter, safe_client
from app.evals.run import create_runner, run_experiment
from app.observability.tracing import Telemetry

# Capture the explicit artifact destination before conftest isolates FINANCE_*
# for each test. Keep that isolation intact; this is opt-in test output only.
_EVIDENCE_DESTINATION=os.environ.get('FINANCE_PHOENIX_EVIDENCE')

pytestmark=[pytest.mark.phoenix_live,pytest.mark.skipif(os.environ.get('FINANCE_PHOENIX_LIVE') != '1',
    reason='requires explicit FINANCE_PHOENIX_LIVE=1 and existing loopback Phoenix')]


def test_live_registration_project_context_and_duplicate_replay(tmp_path):
    sdk=safe_client()
    telemetry=Telemetry(tmp_path/'probe-telemetry.db')
    with telemetry.story_context(uuid4(),uuid4()):
        with telemetry.span('agent.invoke','finance-media-news-scout',{'agent.name':'foundation'}):
            pass
    records=telemetry.pending_records()
    child=next(r for r in records if r.project.endswith('news-scout'))
    parent=next(r for r in records if r.project.endswith('orchestrator'))
    assert child.trace_id == parent.trace_id and child.parent_span_id == parent.span_id
    report=telemetry.flush(5)
    assert report.exported == 2 and report.pending_count == 0, report
    # Phoenix21 binds a globally unique trace ID to one project. A separate
    # orchestrator trace confirms this project can be created/routed in isolation.
    with telemetry.span('agent.invoke','finance-media-orchestrator',{'agent.name':'foundation'}):
        pass
    assert telemetry.flush(5).exported == 1
    def query(project):
        try:
            return sdk.spans.get_spans(project_identifier=project,trace_ids=[parent.trace_id],timeout=1)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return []
            raise
    def await_spans():
        deadline=time.monotonic()+10
        while time.monotonic() < deadline:
            found={r.project:query(r.project) for r in records}
            if {s['context']['span_id'] for rows in found.values() for s in rows} == {parent.span_id,child.span_id}:
                return found
            time.sleep(.2)
        pytest.fail('probe span identities not ingested')
    found=await_spans()
    for record in records:
        assert any(s['context']['span_id'] == record.span_id for rows in found.values() for s in rows)
    received_child=next(s for rows in found.values() for s in rows if s['context']['span_id'] == child.span_id)
    assert received_child['parent_id'] == parent.span_id
    before={project:len(rows) for project,rows in found.items()}
    for record in records:
        assert telemetry.enqueue(record).accepted
    replay=telemetry.flush(5)
    assert replay.exported == 2 and replay.pending_count == 0
    time.sleep(.5)
    after={project:len(query(project)) for project in before}
    projects={project:sdk.projects.get(project_name=project)['id'] for project in before}
    dataset=load_dataset(Path('evals/foundation/1.0.0'),'dev','gate')
    runner=create_runner(tmp_path/'runtime')
    try:
        bundle=run_experiment(ExperimentConfig(),dataset,runner,foundation_evaluators())
        adapter=PhoenixAdapter(tmp_path/'registry.db',client=sdk)
        registration=adapter.register_dataset(dataset)
        published=adapter.publish_experiment(bundle)
        assert registration.status == published.status == 'registered', (registration,published)
        assert adapter.publish_experiment(bundle) == published
        destination=Path(_EVIDENCE_DESTINATION or str(tmp_path/'live-evidence.json'))
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps({'schema_version':'1.0',
            'projects':projects,'trace_id':parent.trace_id,'parent_span_id':parent.span_id,
            'child_span_id':child.span_id,'child_parent_span_id':child.parent_span_id,
            'counts_before_replay':before,'counts_after_replay':after,
            'project_routing_limitation':'Phoenix21 globally unique trace_id belongs to one project',
            'dataset':registration.model_dump(mode='json'),'experiment':published.model_dump(mode='json'),
            'ui_unified_tree':'not_observed_by_test','span_delivery':'at_least_once'},sort_keys=True))
    finally:
        telemetry.shutdown(0)
        runner.telemetry.shutdown(0)
        runner.store.conn.close()
