"""Real synthetic experiment evidence and trusted test-session seam."""
from pathlib import Path
import pytest
from app.contracts.common import Principal
from app.evals.datasets import load_dataset
from app.evals.evaluators import foundation_evaluators
from app.evals.models import ExperimentConfig
from app.evals.run import create_runner, run_experiment, persist_bundle

HUMAN = Principal(provider='github', account_id=12, kind='human', session_id='trusted-test-session')
AGENT = Principal(provider='local', account_id=8, kind='agent')

def trusted(principal):
    return principal == HUMAN

@pytest.fixture
def evidence(tmp_path):
    runner = create_runner(tmp_path/'runtime')
    bundles = {}
    for split in ('dev', 'holdout', 'prod_regression'):
        bundles[split] = run_experiment(ExperimentConfig(repeats=2),
            load_dataset(Path('evals/foundation/1.0.0'),split,'gate'),runner,foundation_evaluators())
    runner.store.conn.close()
    return bundles

def candidate_for(bundles, root):
    from app.release.models import ReleaseCandidate, EvidencePin
    return ReleaseCandidate(evidence={split:EvidencePin(path=str(persist_bundle(bundle,root)),
        bundle_hash=bundle.record_hash()) for split,bundle in bundles.items()})
