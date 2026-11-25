"""Deterministic Scout checks and an anchored editorial rubric."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import combinations
from statistics import mean

from pydantic import ValidationError

from app.evals.scout_contracts import DIMENSIONS, Dimension, Example, SavedScoutOutput, SourcePin

CHECKS = ("contract", "metadata", "reference_integrity", "deterministic_policy")


@dataclass(frozen=True)
class Issue:
    check: str
    code: str
    critical: bool


@dataclass
class CaseScore:
    example_id: str
    issues: list[Issue] = field(default_factory=list)
    rubric: dict[Dimension, float] = field(default_factory=dict)
    correct_pairs: int = 0
    predicted_pairs: int = 0
    expected_pairs: int = 0
    missed_critical_pairs: tuple[tuple[str, str], ...] = ()

    def flag(self, check: str, code: str, *, critical: bool = False) -> None:
        issue = Issue(check, code, critical)
        if issue not in self.issues:
            self.issues.append(issue)

    def checks(self) -> dict[str, bool]:
        failed = {issue.check for issue in self.issues}
        checked = CHECKS if "contract" not in failed else CHECKS[:1]
        # Invalid serialized output cannot establish the downstream checks.
        return {name: name in checked and name not in failed for name in CHECKS}

    def report(self) -> dict[str, object]:
        return {
            "example_id": self.example_id,
            "checks": self.checks(),
            "rubric": self.rubric,
            "duplicate_pairs": {
                "correct": self.correct_pairs,
                "predicted": self.predicted_pairs,
                "expected": self.expected_pairs,
            },
            "missed_critical_pairs": self.missed_critical_pairs,
            "issues": [asdict(issue) for issue in self.issues],
        }


def _pairs(ids: set[str]) -> set[tuple[str, str]]:
    return set(combinations(sorted(ids), 2))


def _f1(actual: set[str], expected: set[str]) -> float:
    if not actual and not expected:
        return 1.0
    return 2 * len(actual & expected) / (len(actual) + len(expected))


def score_case(example: Example, serialized: bytes | None) -> CaseScore:
    result = CaseScore(example.example_id)
    expected = {row.source.source_id: row for row in example.observations}
    groups = {
        event.event_id: {key for key, row in expected.items() if row.event == event.event_id}
        for event in example.events
    }
    truth_pairs = {pair for ids in groups.values() for pair in _pairs(ids)}
    critical_pairs = {
        pair for event in example.events if event.critical_duplicates for pair in _pairs(groups[event.event_id])
    }
    result.expected_pairs = len(truth_pairs)
    try:
        output = SavedScoutOutput.model_validate_json(serialized or b"")
    except ValidationError:
        code = "missing_output" if serialized is None else "invalid_contract"
        result.flag("contract", code, critical=True)
        if truth_pairs:
            result.flag("duplicate_recall", "missed_duplicate")
        result.missed_critical_pairs = tuple(sorted(critical_pairs))
        if result.missed_critical_pairs:
            result.flag("duplicate_recall", "missed_critical_duplicate", critical=True)
        if example.events:
            result.rubric = dict.fromkeys(DIMENSIONS, 0.0)
        return result

    observed = {row.source.source_id: row for row in output.observations}
    if len(observed) != len(output.observations):
        result.flag("metadata", "duplicate_observation", critical=True)
    if set(observed) != set(expected):
        result.flag("metadata", "observation_membership_mismatch", critical=True)
    for key in sorted(observed.keys() & expected.keys()):
        if observed[key].source != expected[key].source:
            result.flag("metadata", "source_metadata_mismatch", critical=True)
        if observed[key].disposition != expected[key].disposition:
            result.flag("deterministic_policy", "wrong_disposition", critical=True)

    failures = sum(row.disposition == "inaccessible" for row in example.observations)
    expected_outcome = "complete"
    if failures:
        expected_outcome = "failed" if failures == len(expected) else "partial"
    if output.outcome != expected_outcome:
        result.flag("deterministic_policy", "wrong_run_outcome", critical=True)

    pins = {
        key: SourcePin(source_id=key, revision=row.source.revision, sha256=row.source.record_hash())
        for key, row in expected.items()
        if key in observed and observed[key].source == row.source
    }
    if len({candidate.story_id for candidate in output.candidates}) != len(output.candidates):
        result.flag("deterministic_policy", "repeated_story_id", critical=True)

    predicted_pairs: set[tuple[str, str]] = set()
    memberships: Counter[str] = Counter()
    event_scores: dict[str, list[dict[Dimension, float]]] = {key: [] for key in groups}
    for candidate in output.candidates:
        visible_text = (candidate.story_id, *candidate.entities, *(claim.text for claim in candidate.claims))
        if any(marker.casefold() in text.casefold() for marker in example.forbidden_output for text in visible_text):
            result.flag("deterministic_policy", "forbidden_output_text", critical=True)
        member_ids = {pin.source_id for pin in candidate.members}
        memberships.update(member_ids)
        predicted_pairs.update(_pairs(member_ids))
        if len(member_ids) != len(candidate.members):
            result.flag("reference_integrity", "duplicate_member", critical=True)
        if candidate.representative not in candidate.members:
            result.flag("reference_integrity", "representative_not_member", critical=True)
        if any(pins.get(pin.source_id) != pin for pin in candidate.members):
            result.flag("reference_integrity", "invalid_source_pin", critical=True)
        if any(key not in expected or expected[key].disposition != "candidate" for key in member_ids):
            result.flag("deterministic_policy", "ineligible_candidate_member", critical=True)

        candidate_events = [event for event in example.events if groups[event.event_id] & member_ids]
        anchors = [claim for event in candidate_events for claim in event.claims]
        supported_anchors: set[int] = set()
        for claim in candidate.claims:
            claim_refs = set(claim.source_ids)
            if not claim_refs <= member_ids & expected.keys() or len(claim_refs) != len(claim.source_ids):
                result.flag("reference_integrity", "foreign_reference", critical=True)
                result.flag("deterministic_policy", "unsupported_claim", critical=True)
                continue
            matching = {
                index for index, anchor in enumerate(anchors)
                if claim.text == anchor.text and claim_refs == set(anchor.source_ids)
            }
            supported_anchors.update(matching)
            if not matching:
                result.flag("deterministic_policy", "unsupported_claim", critical=True)

        if len(candidate_events) != 1:
            result.flag("deterministic_policy", "candidate_event_mismatch")
            continue
        event = candidate_events[0]
        if candidate.revision != event.candidate_revision:
            result.flag("metadata", "candidate_revision_mismatch", critical=True)
        required_warnings = set(event.warnings)
        actual_warnings = set(candidate.warnings)
        if required_warnings - actual_warnings:
            result.flag("deterministic_policy", "required_warning_missing", critical=True)
        if (actual_warnings - required_warnings) & {"correction", "conflict"}:
            result.flag("deterministic_policy", "unsupported_warning", critical=True)
        event_scores[event.event_id].append({
            "audience_relevance": _f1(set(candidate.topics), set(event.topics)),
            "summary_fidelity": 2 * len(supported_anchors) / (len(anchors) + len(candidate.claims)),
            "entity_extraction": _f1(set(candidate.entities), set(event.entities)),
            "uncertainty": _f1(actual_warnings, required_warnings),
        })

    if any(count > 1 for count in memberships.values()):
        result.flag("deterministic_policy", "member_delivered_multiple_times", critical=True)
    if any(row.event is not None and key not in memberships for key, row in expected.items()):
        result.flag("deterministic_policy", "eligible_member_missing")

    result.predicted_pairs = len(predicted_pairs)
    result.correct_pairs = len(truth_pairs & predicted_pairs)
    if predicted_pairs - truth_pairs:
        result.flag("duplicate_precision", "false_duplicate")
    missed = truth_pairs - predicted_pairs
    if missed:
        result.flag("duplicate_recall", "missed_duplicate")
    result.missed_critical_pairs = tuple(sorted(critical_pairs & missed))
    if result.missed_critical_pairs:
        result.flag("duplicate_recall", "missed_critical_duplicate", critical=True)

    if event_scores:
        # Each gold event contributes once; an unresolved split gets its weakest score.
        result.rubric = {
            dimension: mean(
                min((score[dimension] for score in candidates), default=0.0)
                for candidates in event_scores.values()
            )
            for dimension in DIMENSIONS
        }
    return result


def aggregate(results: list[CaseScore]) -> tuple[dict[str, float | None], dict[str, int]]:
    metrics: dict[str, float | None] = {
        check: sum(row.checks()[check] for row in results) / len(results) for check in CHECKS
    }
    for dimension in DIMENSIONS:
        scores = [row.rubric[dimension] for row in results if dimension in row.rubric]
        metrics[dimension] = mean(scores) if scores else None
    pairs = {
        "correct": sum(row.correct_pairs for row in results),
        "predicted": sum(row.predicted_pairs for row in results),
        "expected": sum(row.expected_pairs for row in results),
    }
    metrics["duplicate_precision"] = pairs["correct"] / pairs["predicted"] if pairs["predicted"] else None
    metrics["duplicate_recall"] = pairs["correct"] / pairs["expected"] if pairs["expected"] else None
    return metrics, pairs
