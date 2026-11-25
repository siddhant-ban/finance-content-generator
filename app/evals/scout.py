"""Run a pinned, offline evaluation of persisted News Scout outputs."""

import argparse
import hashlib
import json
from pathlib import Path
from statistics import mean

from pydantic import JsonValue, TypeAdapter, ValidationError

from app.evals.scout_contracts import (
    COMPARABLE, DIMENSIONS, VERSION, Example, Manifest, Policy, SavedExperiment,
)
from app.evals.scout_scoring import CHECKS, CaseScore, aggregate, score_case

EXAMPLES = TypeAdapter(tuple[Example, ...])


class EvaluationInputError(ValueError):
    """Machine-readable input error without any source or label text."""


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate_json_key")
    return result


def _canonical(value: JsonValue) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _read(path: Path, error: str) -> bytes:
    try:
        return _canonical(json.loads(path.read_bytes(), object_pairs_hook=_unique_object))
    except (OSError, ValueError, RecursionError):
        raise EvaluationInputError(error) from None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pinned(path: Path, digest: str, error: str) -> bytes:
    content = _read(path, error)
    if _sha(content) != digest:
        raise EvaluationInputError(error)
    return content


def _examples(content: bytes, manifest: Manifest) -> tuple[Example, ...]:
    rows = EXAMPLES.validate_json(content)
    if not rows or any(row.label_origin != manifest.kind for row in rows):
        raise EvaluationInputError("missing_examples_or_label_origin_mismatch")
    return rows


def _check_split_integrity(splits: dict[str, tuple[Example, ...]]) -> None:
    example_ids: set[str] = set()
    owners: dict[tuple[str, str], str] = {}
    for split, rows in splits.items():
        for row in rows:
            if row.example_id in example_ids:
                raise EvaluationInputError("duplicate_example_id")
            example_ids.add(row.example_id)
            identities = [
                *(("event", event.event_id) for event in row.events),
                *(("near_duplicate", event.near_duplicate_group) for event in row.events),
                *(("near_duplicate", group) for group in row.related_event_groups),
                *(("source_fingerprint", item.source.fingerprint) for item in row.observations),
                *(("source_url", str(item.source.url)) for item in row.observations),
                *(("source_id", item.source.source_id) for item in row.observations),
            ]
            for identity in identities:
                if owners.setdefault(identity, split) != split:
                    raise EvaluationInputError("cross_split_leakage")


def _read_experiment(path: Path) -> tuple[SavedExperiment, str]:
    content = _read(path, "invalid_experiment_file")
    return SavedExperiment.model_validate_json(content), _sha(content)


def _validate_experiment(
    experiment: SavedExperiment, manifest: Manifest, manifest_hash: str, split: str
) -> None:
    if (
        experiment.dataset_version != manifest.dataset_version
        or experiment.dataset_manifest_sha256 != manifest_hash
        or experiment.evaluator_version != manifest.evaluator_version
        or experiment.split != split
    ):
        raise EvaluationInputError("experiment_identity_mismatch")


def _score(experiment: SavedExperiment, examples: tuple[Example, ...]) -> list[CaseScore]:
    if experiment.outputs.keys() - {row.example_id for row in examples}:
        raise EvaluationInputError("unknown_example_id")
    return [
        score_case(row, _canonical(experiment.outputs[row.example_id]) if row.example_id in experiment.outputs else None)
        for row in examples
    ]


def _calibration(
    examples: tuple[Example, ...], experiment: SavedExperiment | None, policy: Policy
) -> dict[str, object]:
    labeled = {row.example_id: row.editor_score for row in examples if row.editor_score is not None}
    if experiment is None or len(labeled) < policy.calibration_min_examples:
        return {"status": "pending", "editor_labels": len(labeled), "matched_examples": 0}
    differences: dict[str, list[float]] = {key: [] for key in DIMENSIONS}
    for row in _score(experiment, examples):
        label = labeled.get(row.example_id)
        if label is None:
            continue
        if not row.rubric or not row.checks()["contract"] or _sha(
            _canonical(experiment.outputs.get(row.example_id))
        ) != label.output_sha256:
            return {
                "status": "fail", "editor_labels": len(labeled),
                "reason": "reviewed_output_missing_or_changed",
            }
        for dimension in DIMENSIONS:
            differences[dimension].append(abs(row.rubric[dimension] - label.scores[dimension]))
    errors = {key: mean(values) for key, values in differences.items()}
    return {
        "status": "pass" if max(errors.values()) <= policy.calibration_max_mean_error else "fail",
        "editor_labels": len(labeled),
        "matched_examples": len(labeled),
        "mean_absolute_error": errors,
    }


def evaluate_experiment(
    dataset: Path,
    outputs: Path,
    *,
    baseline: Path | None = None,
    holdout_file: Path | None = None,
    calibration_outputs: Path | None = None,
) -> dict[str, object]:
    """Return local scoring evidence. Holdout reports expose only aggregate evidence."""
    manifest_content = _read(dataset / "manifest.json", "invalid_dataset_manifest")
    manifest_hash = _sha(manifest_content)
    manifest = Manifest.model_validate_json(manifest_content)
    policy = Policy.model_validate_json(_pinned(dataset / "policy.json", manifest.policy_sha256, "policy_pin_mismatch"))
    experiment, experiment_hash = _read_experiment(outputs)
    _validate_experiment(experiment, manifest, manifest_hash, experiment.split)
    splits = {
        "dev": _examples(_pinned(dataset / "dev.json", manifest.dev_sha256, "dev_pin_mismatch"), manifest),
        "prod_regression": _examples(
            _pinned(dataset / "prod_regression.json", manifest.prod_regression_sha256, "regression_pin_mismatch"),
            manifest,
        ),
    }
    _check_split_integrity(splits)

    calibration_experiment = None
    if calibration_outputs is not None:
        calibration_experiment, _ = _read_experiment(calibration_outputs)
        _validate_experiment(calibration_experiment, manifest, manifest_hash, "dev")
    calibration = _calibration(splits["dev"], calibration_experiment, policy)
    if experiment.split == "holdout":
        if manifest.holdout.status != "sealed" or holdout_file is None:
            raise EvaluationInputError("holdout_unavailable")
        if calibration["status"] != "pass":
            raise EvaluationInputError("dev_calibration_required")
        splits["holdout"] = _examples(
            _pinned(holdout_file, manifest.holdout.sha256, "holdout_pin_mismatch"), manifest,
        )
        _check_split_integrity(splits)
    elif holdout_file is not None:
        raise EvaluationInputError("holdout_file_requires_holdout_experiment")

    examples = splits[experiment.split]
    results = _score(experiment, examples)
    metrics, pairs = aggregate(results)
    critical = sum(issue.critical for row in results for issue in row.issues)
    reasons = [check for check in CHECKS if metrics[check] != 1]
    precision = metrics["duplicate_precision"]
    if precision is None or precision < 0.95:
        reasons.append("duplicate_precision_below_floor")
    for dimension, floor in policy.rubric_floors.items():
        value = metrics[dimension]
        if value is None or value < floor:
            reasons.append(f"{dimension}_below_floor")
    if critical:
        reasons.append("critical_failures")

    comparison = None
    if baseline is not None:
        base, base_hash = _read_experiment(baseline)
        _validate_experiment(base, manifest, manifest_hash, experiment.split)
        old_metrics, _ = aggregate(_score(base, examples))
        regressions = []
        unavailable = []
        for metric in COMPARABLE:
            old, new = old_metrics[metric], metrics[metric]
            if old is None or new is None:
                unavailable.append(metric)
            elif old - new > policy.regression_tolerances[metric] + 1e-12:
                regressions.append(metric)
        if regressions or unavailable:
            reasons.append("baseline_comparison_failed")
        comparison = {"experiment_sha256": base_hash, "regressions": regressions, "unavailable": unavailable}

    issue_counts: dict[str, int] = {}
    for row in results:
        for issue in row.issues:
            issue_counts[issue.code] = issue_counts.get(issue.code, 0) + 1
    return {
        "status": "FAIL" if reasons else "PASS",
        "reasons": reasons,
        "agent": manifest.agent,
        "dataset_version": manifest.dataset_version,
        "dataset_kind": manifest.kind,
        "dataset_manifest_sha256": manifest_hash,
        "evaluator_version": VERSION,
        "policy_version": policy.policy_version,
        "policy_sha256": manifest.policy_sha256,
        "experiment_sha256": experiment_hash,
        "model_id": experiment.model_id,
        "prompt_version": experiment.prompt_version,
        "split": experiment.split,
        "holdout": manifest.holdout.status,
        "release_eligible": False,
        "editor_calibration": calibration,
        "example_count": len(examples),
        "metrics": metrics,
        "duplicate_pairs": pairs,
        "critical_count": critical,
        "issue_counts": issue_counts,
        "comparison": comparison,
        "examples": [row.report() for row in results] if experiment.split != "holdout" else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--holdout-file", type=Path)
    parser.add_argument("--calibration-outputs", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate_experiment(
            args.dataset, args.outputs, baseline=args.baseline,
            holdout_file=args.holdout_file, calibration_outputs=args.calibration_outputs,
        )
    except EvaluationInputError as error:
        print(json.dumps({"status": "ERROR", "reason": str(error)}))
        return 2
    except ValidationError:
        print(json.dumps({"status": "ERROR", "reason": "invalid_scoring_input"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
