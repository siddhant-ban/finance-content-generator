"""Exact compatible synthetic comparison, without baseline approval authority."""
import argparse
import json
from pathlib import Path

from app.evals.models import ComparisonReport, ExperimentBundle
from app.evals.run import exit_code, read_bundle, validate_bundle

COMPATIBLE_PINS = ('dataset_hash','dataset_version','schema_hash','policy_hash','policy_version',
                   'evaluator_hash','evaluator_implementation_hash')


def compare(candidate: ExperimentBundle, baseline: ExperimentBundle) -> ComparisonReport:
    candidate_hash, baseline_hash = candidate.record_hash(), baseline.record_hash()
    def report(status, reason, failures=()):
        return ComparisonReport(status=status,reason=reason,candidate_hash=candidate_hash,
            baseline_hash=baseline_hash,failing_examples=failures)
    try:
        candidate, baseline = validate_bundle(candidate), validate_bundle(baseline)
    except ValueError:
        return report('ERROR','invalid_bundle')
    if (any(candidate.provenance.get(key) is None or candidate.provenance.get(key) != baseline.provenance.get(key) for key in COMPATIBLE_PINS)
            or candidate.evaluator_versions != baseline.evaluator_versions
            or candidate.split != baseline.split or candidate.config.repeats != baseline.config.repeats
            or candidate.config.seed != baseline.config.seed or baseline.status != 'PASS'
            or candidate.expected_count != baseline.expected_count
            or {(r.example_id,r.repeat) for r in candidate.records} != {(r.example_id,r.repeat) for r in baseline.records}):
        return report('ERROR','not_comparable')
    if candidate.status in {'ERROR','INCOMPLETE'}:
        return report('ERROR','candidate_failed')
    if candidate.status == 'FAIL':
        return report('FAIL','candidate_failed',tuple(sorted({row.example_id for row in candidate.private_failures()})))
    return report('PASS','compatible')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Compare synthetic fixtures; no production baseline approval.')
    parser.add_argument('--agent',required=True,choices=['foundation'])
    parser.add_argument('--candidate',required=True,type=Path)
    parser.add_argument('--baseline',required=True)
    parser.add_argument('--registry',type=Path,default=None)
    args = parser.parse_args(argv)
    try:
        candidate = read_bundle(args.candidate)
        if args.baseline == 'approved':
            from app.config import Settings
            from app.release.service import ReleaseService
            import sqlite3
            registry=args.registry or Settings.from_env().data_dir/'state.db'
            conn=sqlite3.connect(registry.resolve().as_uri()+'?mode=ro',uri=True)
            try:
                baseline=ReleaseService(conn).approved_baseline(args.agent,candidate.split)
            finally:
                conn.close()
        else:
            baseline=read_bundle(Path(args.baseline))
        result = compare(candidate,baseline)
        print(json.dumps(result.summary(),sort_keys=True))
        return exit_code(result.status)
    except Exception:
        print(json.dumps({'status':'ERROR','reason':'invalid_bundle'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
