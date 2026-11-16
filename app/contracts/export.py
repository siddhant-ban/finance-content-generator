"""Deterministic checked-in schema export; run with --out schemas/v1."""
import argparse
import json
from pathlib import Path
from app.contracts import common, execution, evidence, workflow
from app.evals import models as evaluations
from app.execution import protocols
from app.workflow import state
from app.observability import models as telemetry
from app.release import models as releases

from app.review.service import HumanDecision

GROUPS = {
    "releases.json": (releases.ReleasePolicy,releases.EvidencePin,releases.ReleaseCandidate,
                      releases.GateDecision,releases.ReleaseApproval,releases.FeedbackAnnotation,
                      releases.FeedbackCandidate),
    "evaluations.json": (evaluations.FixtureInput, evaluations.FixtureReference, evaluations.EvalExample,
                         evaluations.EvalResult, evaluations.EvaluationPolicy, evaluations.ExperimentConfig,
                         evaluations.ExperimentBundle, evaluations.ComparisonReport),
    "telemetry.json": (telemetry.SpanRecord, telemetry.SpanEvent, telemetry.EnqueueResult,
                       telemetry.HealthReport, telemetry.ExportReport, telemetry.CollectorHealth),
    "state.json": (state.StateEvent,state.StorySnapshot,state.TransitionRequest),
    "common.json": (common.ArtifactRef, common.Money, common.Percent, common.PercentagePoints, common.Usage, common.Principal),
    "execution.json": (execution.InputEnvelope, execution.OutputEnvelope, execution.ExecutionError,
                       execution.AttemptRecord, protocols.ModelConfiguration, protocols.ModelRequest, protocols.ModelResponse,
                       execution.SyntheticData, protocols.ModelRequest[execution.SyntheticData],
                       protocols.ModelResponse[execution.SyntheticData]),
    "evidence.json": (evidence.SourceRecord,evidence.ClaimRevision,evidence.EvidenceRef,evidence.VerificationRecord,evidence.NumericObservation,evidence.ClaimUse),
    "workflow.json": (workflow.ReviewManifest,workflow.PolicyReport,workflow.RevisionRef),
}

def export_schemas(directory: Path) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    for name, models in GROUPS.items():
        content = {"schema_version": "1.0", "models": {model.__name__: model.model_json_schema() for model in models}}
        (directory/name).write_text(json.dumps(content,sort_keys=True,indent=2)+"\n")
    # The published review contract is a standalone JSON Schema.
    (directory/"review.json").write_text(json.dumps(HumanDecision.model_json_schema(),sort_keys=True,indent=2)+"\n")
    return [*GROUPS, "review.json"]

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    export_schemas(args.out)
