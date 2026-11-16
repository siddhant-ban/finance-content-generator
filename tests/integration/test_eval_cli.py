"""Invoke real local CLI; output must not disclose holdout content."""
import json
from pathlib import Path
import subprocess
import sys


def cli(module, *args):
    return subprocess.run([sys.executable, '-m', module, *map(str,args)], capture_output=True, text=True)


def test_cli_exit_codes_and_holdout_redaction(tmp_path):
    result = cli('app.evals.run', '--agent', 'foundation', '--dataset', 'evals/foundation/1.0.0', '--split', 'holdout', '--output', tmp_path)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary['status'] == 'PASS' and summary['synthetic'] is True and summary['example_count'] == 3
    assert 'fixture-source' not in result.stdout and 'reference' not in result.stdout
    assert 'foundation-holdout' not in result.stdout
    bundle = Path(summary['bundle_path'])
    assert json.loads(bundle.read_text())['bundle']['records']
    compared = cli('app.evals.compare', '--agent', 'foundation', '--candidate', bundle, '--baseline', bundle)
    assert compared.returncode == 0
    missing = cli('app.evals.run', '--agent', 'foundation', '--dataset', tmp_path/'absent', '--split', 'dev', '--output', tmp_path)
    assert missing.returncode == 2 and 'Traceback' not in missing.stderr
    broken = tmp_path/'broken.json'; broken.write_text('{}')
    assert cli('app.evals.compare', '--agent','foundation','--candidate',broken,'--baseline',bundle).returncode == 2


def test_cli_failed_candidate_exit_one_and_incomplete_exit_two(tmp_path):
    from app.evals.models import EvalResult
    from app.evals.run import read_bundle, persist_bundle, aggregate, assess
    run = cli('app.evals.run','--agent','foundation','--dataset','evals/foundation/1.0.0',
        '--split','dev','--output',tmp_path)
    baseline_path = Path(json.loads(run.stdout)['bundle_path'])
    baseline = read_bundle(baseline_path)
    first = baseline.records[0]
    failed = first.results[1].model_copy(update={'status':'FAIL','value':0.0})
    records = (first.model_copy(update={'results':(first.results[0],failed,*first.results[2:])}),*baseline.records[1:])
    candidate = baseline.model_copy(update={'records':records,'status':'FAIL','aggregates':aggregate(records)})
    failed_path = persist_bundle(candidate,tmp_path)
    result = cli('app.evals.compare','--agent','foundation','--candidate',failed_path,'--baseline',baseline_path)
    assert result.returncode == 1 and json.loads(result.stdout)['status'] == 'FAIL'
    assert 'foundation-dev-1' not in result.stdout
    missing_results = (first.model_copy(update={'results':first.results[:-1]}),*baseline.records[1:])
    status, critical, issues = assess(missing_results,3,1,())
    incomplete = baseline.model_copy(update={'records':missing_results,'status':status,
        'issues':issues,'aggregates':aggregate(missing_results)})
    incomplete_path = persist_bundle(incomplete,tmp_path)
    assert cli('app.evals.compare','--agent','foundation','--candidate',incomplete_path,'--baseline',baseline_path).returncode == 2
