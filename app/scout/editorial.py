"""Editorial assignment decisions, distinct-story pilot yield, cost reporting, and gated failure export."""
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, model_validator

from app.contracts.common import ArtifactRef, Nonempty, Sha256, StrictModel, UtcDatetime
from app.contracts.scout import ScoutRunOutput, StoryCandidate, Topic, Warning
from app.scout.eval_config import NonNegativeDecimal
from app.scout.ledger import pico_to_usd, usd_to_pico_ceil
from app.storage.artifacts import ArtifactStore

EditorialDecisionOutcome = Literal["assigned", "passed"]
EditorialStatus = Literal["assigned", "passed", "pending"]
CostKind = Literal["measured", "estimated", "missing", "released"]
RunOutcome = Literal["complete", "empty", "partial", "failed"]

DEFAULT_EXCLUDED_COSTS: tuple[str, ...] = (
    "editorial_review_labor",
    "local_compute_energy",
    "infrastructure_overhead",
)
DEFAULT_EXCLUDED_COSTS_NOTE = (
    "Model API/token costs only; human editorial labor, local compute energy, "
    "and infrastructure overhead are excluded."
)

_MAX_EXPORT_EXCERPT_CHARS = 500
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:bearer|api[_-]?key|secret|password)\s*[:=]\s*\S+"),
)
_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"),
)


class EditorialDecisionError(ValueError):
    """Raised when an editorial assignment/pass decision violates provenance or contract bindings."""


class ExportGateError(ValueError):
    """Raised when adjudicated failure export fails licensing, privacy, or dataset-boundary gates."""


class EditorialDecision(StrictModel):
    """Immutable append-only editorial assignment or pass decision bound to an exact Scout candidate."""

    decision_id: UUID
    story_id: UUID
    revision: int = Field(ge=1)
    run_id: UUID
    candidate_sha256: Sha256
    decision: EditorialDecisionOutcome
    reason_code: Nonempty
    notes: str = Field(default="", max_length=8000)
    actor_id: Nonempty
    decided_at: UtcDatetime

    @model_validator(mode="after")
    def _non_blank_metadata(self):
        if not self.reason_code.strip():
            raise ValueError("reason_code must be non-blank")
        if not self.actor_id.strip():
            raise ValueError("actor_id must be non-blank")
        return self


class EditorialCandidateSummary(StrictModel):
    """Editor inspection view of a persisted Scout candidate and its computed editorial status."""

    story_id: UUID
    revision: int = Field(ge=1)
    run_id: UUID
    candidate_sha256: Sha256
    status: EditorialStatus
    headline: str
    publisher: str
    topics: tuple[Topic, ...]
    warnings: tuple[Warning, ...]
    decisions: tuple[EditorialDecision, ...] = ()


class PilotRunCandidate(StrictModel):
    """Candidate observed or created in a pilot run along with contributing feed IDs."""

    story_id: UUID
    revision: int = Field(ge=1)
    candidate_sha256: Sha256
    feed_ids: tuple[Nonempty, ...] = ()


class RunCostAttempt(StrictModel):
    """Recorded per-attempt model cost entry attached to a pilot run."""

    attempt_id: Nonempty
    cost_kind: CostKind
    cost_usd: NonNegativeDecimal | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    model_id: Nonempty | None = None
    reservation_id: Nonempty | None = None
    reason: Nonempty | None = None

    @model_validator(mode="after")
    def _validate_cost_kind(self):
        if self.cost_kind in {"measured", "estimated"} and self.cost_usd is None:
            raise ValueError("measured and estimated cost records require cost_usd")
        if self.cost_kind == "missing" and self.cost_usd is not None:
            raise ValueError("missing cost records cannot supply cost_usd")
        return self


class PilotRunRecord(StrictModel):
    """Persisted pilot run envelope retaining empty and failed runs in the denominator."""

    run_id: UUID
    recorded_at: UtcDatetime
    outcome: RunOutcome
    feed_ids: tuple[Nonempty, ...] = ()
    candidate_feeds: tuple[PilotRunCandidate, ...] = ()
    experiment_id: Nonempty | None = None
    example_id: Nonempty | None = None
    reservation_ids: tuple[Nonempty, ...] = ()
    reservation_cost_kinds: dict[Nonempty, Literal["measured", "estimated"]] = Field(default_factory=dict)
    cost_records: tuple[RunCostAttempt, ...] = ()


class FeedYieldSummary(StrictModel):
    """Unique candidate and assignment yield for one contributing/polled feed."""

    feed_id: Nonempty
    runs_participated: int = Field(ge=0)
    unique_candidates: int = Field(ge=0)
    reviewed_candidates: int = Field(ge=0)
    pending_candidates: int = Field(ge=0)
    unique_assignments: int = Field(ge=0)
    assignment_yield: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None


class ChannelTotals(StrictModel):
    """Deduplicated channel-wide editorial yield across all selected pilot runs."""

    total_runs: int = Field(ge=0)
    complete_runs: int = Field(ge=0)
    partial_runs: int = Field(ge=0)
    empty_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    assignment_producing_runs: int = Field(ge=0)
    total_candidates: int = Field(ge=0)
    distinct_assigned: int = Field(ge=0)
    distinct_passed: int = Field(ge=0)
    reviewed_stories: int = Field(ge=0)
    pending_stories: int = Field(ge=0)
    review_coverage: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    assigned_per_reviewed: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    assignment_yield: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    total_decision_events: int = Field(ge=0)


class PilotCostReport(StrictModel):
    """Explicitly classified model cost report and excluded-cost disclosure."""

    measured_cost_usd: NonNegativeDecimal
    estimated_cost_usd: NonNegativeDecimal
    total_model_cost_usd: NonNegativeDecimal
    ambiguous_or_pending_reserved_usd: NonNegativeDecimal
    measured_cost_attempts: int = Field(ge=0)
    estimated_cost_attempts: int = Field(ge=0)
    missing_cost_attempts: int = Field(ge=0)
    released_attempts: int = Field(ge=0)
    total_attempts: int = Field(ge=0)
    cost_per_distinct_assignment_usd: NonNegativeDecimal | None = None
    excluded_costs: tuple[Nonempty, ...] = DEFAULT_EXCLUDED_COSTS
    excluded_costs_note: Nonempty = DEFAULT_EXCLUDED_COSTS_NOTE


class PilotReport(StrictModel):
    """Operator pilot report combining distinct-story yield, per-feed yield, and model cost."""

    schema_version: Literal["1.0"] = "1.0"
    window_start: UtcDatetime | None = None
    window_end: UtcDatetime | None = None
    selected_run_ids: tuple[UUID, ...]
    channel_totals: ChannelTotals
    by_feed: tuple[FeedYieldSummary, ...]
    cost: PilotCostReport


class ExportedSourcePin(StrictModel):
    """Rights-checked source provenance pin without full copyrighted article bodies."""

    source_id: UUID
    revision: int = Field(ge=1)
    source_record_hash: Sha256
    publisher: Nonempty
    source_class: Literal["primary", "reputable_secondary", "commentary", "unknown"]
    canonical_url: Nonempty
    rights: Literal["metadata_only", "excerpt_permitted"]
    published_at: UtcDatetime | None
    retrieved_at: UtcDatetime
    locator: str | None = None
    excerpt: Annotated[str, Field(max_length=_MAX_EXPORT_EXCERPT_CHARS)] | None = None


class AdjudicatedFailureRecord(StrictModel):
    """Privacy- and rights-gated editorial pass/failure record for future dataset versions."""

    decision_id: UUID
    story_id: UUID
    revision: int = Field(ge=1)
    run_id: UUID
    candidate_sha256: Sha256
    decision: EditorialDecisionOutcome
    reason_code: Nonempty
    notes: str = ""
    decided_at: UtcDatetime
    topics: tuple[Topic, ...]
    warnings: tuple[Warning, ...]
    source_pins: tuple[ExportedSourcePin, ...]


class AdjudicatedFailuresExport(StrictModel):
    """Gated export bundle of adjudicated editorial failures for future datasets."""

    schema_version: Literal["1.0"] = "1.0"
    target_dataset_version: Nonempty
    exported_at: UtcDatetime
    rights_cleared: Literal[True]
    privacy_checked: Literal[True]
    record_count: int = Field(ge=0)
    records: tuple[AdjudicatedFailureRecord, ...]


_CANDIDATE_FEEDS_ADAPTER = TypeAdapter(tuple[PilotRunCandidate, ...])
_COST_RECORDS_ADAPTER = TypeAdapter(tuple[RunCostAttempt, ...])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _decision_from_row(row: tuple) -> EditorialDecision:
    return EditorialDecision(
        decision_id=UUID(row[0]),
        story_id=UUID(row[1]),
        revision=int(row[2]),
        run_id=UUID(row[3]),
        candidate_sha256=row[4],
        decision=row[5],
        reason_code=row[6],
        notes=row[7],
        actor_id=row[8],
        decided_at=datetime.fromisoformat(row[9]).astimezone(timezone.utc),
    )


def _compute_status(decisions: Sequence[EditorialDecision]) -> EditorialStatus:
    if any(dec.decision == "assigned" for dec in decisions):
        return "assigned"
    if any(dec.decision == "passed" for dec in decisions):
        return "passed"
    return "pending"


def candidate_editorial_status(
    conn: sqlite3.Connection,
    *,
    story_id: UUID,
    revision: int,
) -> EditorialStatus:
    """Compute editorial status ('assigned', 'passed', or 'pending') for a story revision."""
    rows = conn.execute(
        "SELECT decision_id, story_id, revision, run_id, candidate_sha256, "
        "decision, reason_code, notes, actor_id, decided_at "
        "FROM scout_editorial_decisions WHERE story_id = ? AND revision = ? "
        "ORDER BY decided_at ASC, decision_id ASC",
        (str(story_id), revision),
    ).fetchall()
    return _compute_status([_decision_from_row(r) for r in rows])


def list_candidates(
    conn: sqlite3.Connection,
    store: ArtifactStore,
    *,
    run_id: UUID | None = None,
) -> tuple[EditorialCandidateSummary, ...]:
    """List persisted Scout candidates for editor inspection with computed editorial status."""
    query = (
        "SELECT r.story_id, r.revision, r.run_id, r.artifact_sha256, "
        "a.media_type, a.size_bytes, a.schema_version "
        "FROM scout_story_revisions r "
        "JOIN artifacts a ON a.sha256 = r.artifact_sha256 "
    )
    params: tuple[str, ...] = ()
    if run_id is not None:
        query += "WHERE r.run_id = ? "
        params = (str(run_id),)
    query += "ORDER BY r.created_at ASC, r.story_id ASC, r.revision ASC"
    rev_rows = conn.execute(query, params).fetchall()

    decision_rows = conn.execute(
        "SELECT decision_id, story_id, revision, run_id, candidate_sha256, "
        "decision, reason_code, notes, actor_id, decided_at "
        "FROM scout_editorial_decisions ORDER BY decided_at ASC, decision_id ASC"
    ).fetchall()
    by_rev: dict[tuple[str, int], list[EditorialDecision]] = defaultdict(list)
    for row in decision_rows:
        dec = _decision_from_row(row)
        by_rev[(str(dec.story_id), dec.revision)].append(dec)

    summaries: list[EditorialCandidateSummary] = []
    for row in rev_rows:
        ref = ArtifactRef(sha256=row[3], media_type=row[4], size_bytes=row[5], schema_version=row[6])
        cand = StoryCandidate.model_validate_json(store.read(ref))
        decs = tuple(by_rev.get((row[0], int(row[1])), ()))
        rep = cand.representative_member
        summaries.append(
            EditorialCandidateSummary(
                story_id=cand.story_id,
                revision=cand.revision,
                run_id=UUID(row[2]),
                candidate_sha256=ref.sha256,
                status=_compute_status(decs),
                headline=rep.headline,
                publisher=rep.source.publisher,
                topics=cand.topics,
                warnings=cand.warnings,
                decisions=decs,
            )
        )
    return tuple(summaries)


def record_decision(
    conn: sqlite3.Connection,
    *,
    story_id: UUID,
    revision: int,
    run_id: UUID,
    candidate_sha256: str,
    decision: EditorialDecisionOutcome,
    reason_code: str,
    notes: str = "",
    actor_id: str,
    decided_at: datetime | None = None,
) -> EditorialDecision:
    """Append an immutable editorial assignment/pass decision bound to an exact candidate revision."""
    if conn.in_transaction:
        raise ValueError("editorial operation requires an idle connection")
    if decision not in {"assigned", "passed"}:
        raise EditorialDecisionError(f"invalid_editorial_decision: {decision}")

    try:
        record = EditorialDecision(
            decision_id=uuid4(),
            story_id=story_id,
            revision=revision,
            run_id=run_id,
            candidate_sha256=candidate_sha256,
            decision=decision,
            reason_code=reason_code,
            notes=notes,
            actor_id=actor_id,
            decided_at=_normalize_utc(decided_at or _utc_now()),
        )
    except ValueError as err:
        raise EditorialDecisionError(str(err)) from err

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        rev_row = conn.execute(
            "SELECT artifact_sha256, run_id FROM scout_story_revisions WHERE story_id = ? AND revision = ?",
            (str(record.story_id), record.revision),
        ).fetchone()
        if rev_row is None:
            raise EditorialDecisionError("unknown_story_revision")
        if rev_row[0] != record.candidate_sha256:
            raise EditorialDecisionError("candidate_sha256_mismatch")

        known_run = (
            rev_row[1] == str(record.run_id)
            or conn.execute(
                "SELECT 1 FROM scout_run_requests WHERE run_id = ?",
                (str(record.run_id),),
            ).fetchone() is not None
            or conn.execute(
                "SELECT 1 FROM scout_pilot_runs WHERE run_id = ?",
                (str(record.run_id),),
            ).fetchone() is not None
        )
        if not known_run:
            raise EditorialDecisionError("unknown_run_id")

        conn.execute(
            "INSERT INTO scout_editorial_decisions("
            "decision_id, story_id, revision, run_id, candidate_sha256, "
            "decision, reason_code, notes, actor_id, decided_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(record.decision_id),
                str(record.story_id),
                record.revision,
                str(record.run_id),
                record.candidate_sha256,
                record.decision,
                record.reason_code,
                record.notes,
                record.actor_id,
                record.decided_at.isoformat(),
            ),
        )
    return record


def _extract_candidates_from_run_output(
    run_output: ScoutRunOutput,
    store: ArtifactStore,
    *,
    fallback_feed_ids: tuple[str, ...],
    candidate_feeds: Mapping[str, Sequence[str]] | None,
) -> tuple[PilotRunCandidate, ...]:
    feeds_by_sha: dict[str, set[str]] = defaultdict(set)
    if run_output.feeds is not None:
        item_to_feeds: dict[str, set[str]] = defaultdict(set)
        for feed_poll in run_output.feeds:
            for item_id in feed_poll.item_ids:
                item_to_feeds[item_id].add(feed_poll.feed_id)
        for item in run_output.items:
            if item.candidate_ref is not None:
                feeds_by_sha[item.candidate_ref.sha256].update(item_to_feeds.get(item.item_id, ()))

    extracted: list[PilotRunCandidate] = []
    for ref in run_output.candidates:
        cand = StoryCandidate.model_validate_json(store.read(ref))
        explicit = candidate_feeds.get(ref.sha256) if candidate_feeds is not None else None
        if explicit is not None:
            resolved_feeds = tuple(sorted(set(explicit)))
        elif feeds_by_sha.get(ref.sha256):
            resolved_feeds = tuple(sorted(feeds_by_sha[ref.sha256]))
        else:
            resolved_feeds = tuple(sorted(set(fallback_feed_ids)))
        extracted.append(
            PilotRunCandidate(
                story_id=cand.story_id,
                revision=cand.revision,
                candidate_sha256=ref.sha256,
                feed_ids=resolved_feeds,
            )
        )
    return tuple(extracted)


def record_pilot_run(
    conn: sqlite3.Connection,
    *,
    run_id: UUID | None = None,
    run_output: ScoutRunOutput | None = None,
    store: ArtifactStore | None = None,
    recorded_at: datetime | None = None,
    outcome: RunOutcome | None = None,
    feed_ids: Sequence[str] = (),
    candidate_feeds: Mapping[str, Sequence[str]] | Sequence[PilotRunCandidate] | None = None,
    experiment_id: str | None = None,
    example_id: str | None = None,
    reservation_ids: Sequence[str] = (),
    reservation_cost_kinds: Mapping[str, Literal["measured", "estimated"]] | None = None,
    cost_records: Sequence[RunCostAttempt] = (),
) -> PilotRunRecord:
    """Record a selected pilot run (including empty and failed runs) for yield and cost reporting."""
    if conn.in_transaction:
        raise ValueError("pilot run persistence requires an idle connection")

    if run_output is not None:
        resolved_run_id = run_output.run_id
        resolved_outcome: RunOutcome = outcome or run_output.outcome
        inferred_feeds = (
            tuple(feed.feed_id for feed in run_output.feeds)
            if run_output.feeds is not None
            else ()
        )
        resolved_feed_ids = tuple(sorted(set(feed_ids) | set(inferred_feeds)))
        if isinstance(candidate_feeds, Sequence) and not isinstance(candidate_feeds, (str, bytes)):
            resolved_candidates = tuple(candidate_feeds)
        else:
            if run_output.candidates and store is None:
                raise ValueError("store is required to resolve candidate story IDs from run_output")
            resolved_candidates = (
                _extract_candidates_from_run_output(
                    run_output,
                    store,  # type: ignore[arg-type]
                    fallback_feed_ids=resolved_feed_ids,
                    candidate_feeds=candidate_feeds if isinstance(candidate_feeds, Mapping) else None,
                )
                if run_output.candidates
                else ()
            )
    else:
        if run_id is None or outcome is None:
            raise ValueError("run_id and outcome are required when run_output is omitted")
        resolved_run_id = run_id
        resolved_outcome = outcome
        resolved_feed_ids = tuple(sorted(set(feed_ids)))
        if candidate_feeds is None:
            resolved_candidates = ()
        elif isinstance(candidate_feeds, Mapping):
            if store is None:
                raise ValueError("store is required when candidate_feeds is passed as a sha256 mapping")
            resolved_candidates = _build_candidates_from_mapping(conn, store, candidate_feeds)
        else:
            resolved_candidates = tuple(candidate_feeds)

    all_feeds = set(resolved_feed_ids)
    for cand in resolved_candidates:
        all_feeds.update(cand.feed_ids)

    record = PilotRunRecord(
        run_id=resolved_run_id,
        recorded_at=_normalize_utc(recorded_at or _utc_now()),
        outcome=resolved_outcome,
        feed_ids=tuple(sorted(all_feeds)),
        candidate_feeds=resolved_candidates,
        experiment_id=experiment_id,
        example_id=example_id,
        reservation_ids=tuple(dict.fromkeys(reservation_ids)),
        reservation_cost_kinds=dict(reservation_cost_kinds or {}),
        cost_records=tuple(cost_records),
    )

    candidate_payload = json.dumps(
        [c.model_dump(mode="json") for c in record.candidate_feeds],
        sort_keys=True,
        separators=(",", ":"),
    )
    cost_payload = json.dumps(
        [c.model_dump(mode="json") for c in record.cost_records],
        sort_keys=True,
        separators=(",", ":"),
    )

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO scout_pilot_runs("
            "run_id, recorded_at, outcome, feed_ids_json, candidate_feeds_json, "
            "experiment_id, example_id, reservation_ids_json, reservation_cost_kinds_json, cost_records_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(record.run_id),
                record.recorded_at.isoformat(),
                record.outcome,
                json.dumps(list(record.feed_ids)),
                candidate_payload,
                record.experiment_id,
                record.example_id,
                json.dumps(list(record.reservation_ids)),
                json.dumps(record.reservation_cost_kinds, sort_keys=True),
                cost_payload,
            ),
        )
    return record


def _build_candidates_from_mapping(
    conn: sqlite3.Connection,
    store: ArtifactStore,
    candidate_feeds: Mapping[str, Sequence[str]],
) -> tuple[PilotRunCandidate, ...]:
    items: list[PilotRunCandidate] = []
    for sha, fids in sorted(candidate_feeds.items()):
        row = conn.execute(
            "SELECT a.sha256, a.media_type, a.size_bytes, a.schema_version "
            "FROM artifacts a WHERE a.sha256 = ?",
            (sha,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown_candidate_artifact: {sha}")
        ref = ArtifactRef(sha256=row[0], media_type=row[1], size_bytes=row[2], schema_version=row[3])
        cand = StoryCandidate.model_validate_json(store.read(ref))
        items.append(
            PilotRunCandidate(
                story_id=cand.story_id,
                revision=cand.revision,
                candidate_sha256=sha,
                feed_ids=tuple(sorted(set(fids))),
            )
        )
    return tuple(items)


def _load_pilot_runs(
    conn: sqlite3.Connection,
    *,
    run_ids: Sequence[UUID | str] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> tuple[PilotRunRecord, ...]:
    norm_start = _normalize_utc(window_start) if window_start is not None else None
    norm_end = _normalize_utc(window_end) if window_end is not None else None
    wanted_ids = {str(UUID(str(rid))) for rid in run_ids} if run_ids is not None else None

    rows = conn.execute(
        "SELECT run_id, recorded_at, outcome, feed_ids_json, candidate_feeds_json, "
        "experiment_id, example_id, reservation_ids_json, reservation_cost_kinds_json, cost_records_json "
        "FROM scout_pilot_runs"
    ).fetchall()

    selected: list[PilotRunRecord] = []
    for row in rows:
        rec = PilotRunRecord(
            run_id=UUID(row[0]),
            recorded_at=datetime.fromisoformat(row[1]).astimezone(timezone.utc),
            outcome=row[2],
            feed_ids=tuple(json.loads(row[3])),
            candidate_feeds=_CANDIDATE_FEEDS_ADAPTER.validate_json(row[4]),
            experiment_id=row[5],
            example_id=row[6],
            reservation_ids=tuple(json.loads(row[7])),
            reservation_cost_kinds=json.loads(row[8]),
            cost_records=_COST_RECORDS_ADAPTER.validate_json(row[9]),
        )
        if wanted_ids is not None and str(rec.run_id) not in wanted_ids:
            continue
        if norm_start is not None and rec.recorded_at < norm_start:
            continue
        if norm_end is not None and rec.recorded_at >= norm_end:
            continue
        selected.append(rec)

    selected.sort(key=lambda r: (r.recorded_at, str(r.run_id)))
    return tuple(selected)


def _aggregate_run_costs(
    selected_runs: Sequence[PilotRunRecord],
    *,
    ledger_conn: sqlite3.Connection,
) -> PilotCostReport:
    has_ledger_table = (
        ledger_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scout_eval_reservations'"
        ).fetchone()
        is not None
    )

    measured_pico = 0
    estimated_pico = 0
    ambig_reserved_pico = 0
    measured_attempts = 0
    estimated_attempts = 0
    missing_attempts = 0
    released_attempts = 0

    explicit_res_ids_seen: set[str] = set()
    for run in selected_runs:
        for attempt in run.cost_records:
            if attempt.reservation_id:
                explicit_res_ids_seen.add(attempt.reservation_id)
            if attempt.cost_kind == "measured":
                assert attempt.cost_usd is not None
                measured_pico += usd_to_pico_ceil(attempt.cost_usd)
                measured_attempts += 1
            elif attempt.cost_kind == "estimated":
                assert attempt.cost_usd is not None
                estimated_pico += usd_to_pico_ceil(attempt.cost_usd)
                estimated_attempts += 1
            elif attempt.cost_kind == "missing":
                missing_attempts += 1
            elif attempt.cost_kind == "released":
                released_attempts += 1

    if has_ledger_table:
        accounted_res_ids = set(explicit_res_ids_seen)
        for run in selected_runs:
            res_ids = list(run.reservation_ids)
            if run.experiment_id is not None and run.example_id is not None:
                exp_rows = ledger_conn.execute(
                    "SELECT reservation_id FROM scout_eval_reservations "
                    "WHERE experiment_id = ? AND example_id = ?",
                    (run.experiment_id, run.example_id),
                ).fetchall()
                res_ids.extend(r[0] for r in exp_rows)

            for res_id in dict.fromkeys(res_ids):
                if res_id in accounted_res_ids:
                    continue
                accounted_res_ids.add(res_id)
                row = ledger_conn.execute(
                    "SELECT status, reserved_pico, charged_pico FROM scout_eval_reservations "
                    "WHERE reservation_id = ?",
                    (res_id,),
                ).fetchone()
                if row is None:
                    missing_attempts += 1
                    continue
                status, reserved_pico, charged_pico = row
                if status == "reconciled" and charged_pico is not None:
                    kind = run.reservation_cost_kinds.get(res_id, "measured")
                    if kind == "estimated":
                        estimated_pico += int(charged_pico)
                        estimated_attempts += 1
                    else:
                        measured_pico += int(charged_pico)
                        measured_attempts += 1
                elif status in {"pending", "ambiguous"}:
                    ambig_reserved_pico += int(reserved_pico)
                    missing_attempts += 1
                elif status == "released":
                    released_attempts += 1

    measured_usd = pico_to_usd(measured_pico)
    estimated_usd = pico_to_usd(estimated_pico)
    total_usd = pico_to_usd(measured_pico + estimated_pico)
    ambig_usd = pico_to_usd(ambig_reserved_pico)
    total_attempts = measured_attempts + estimated_attempts + missing_attempts + released_attempts

    return PilotCostReport(
        measured_cost_usd=measured_usd,
        estimated_cost_usd=estimated_usd,
        total_model_cost_usd=total_usd,
        ambiguous_or_pending_reserved_usd=ambig_usd,
        measured_cost_attempts=measured_attempts,
        estimated_cost_attempts=estimated_attempts,
        missing_cost_attempts=missing_attempts,
        released_attempts=released_attempts,
        total_attempts=total_attempts,
        cost_per_distinct_assignment_usd=None,
    )


def generate_pilot_report(
    conn: sqlite3.Connection,
    *,
    ledger_conn: sqlite3.Connection | None = None,
    run_ids: Sequence[UUID | str] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> PilotReport:
    """Generate distinct-story editorial yield, per-feed yield, and model cost per assignment report."""
    norm_start = _normalize_utc(window_start) if window_start is not None else None
    norm_end = _normalize_utc(window_end) if window_end is not None else None
    selected_runs = _load_pilot_runs(
        conn,
        run_ids=run_ids,
        window_start=norm_start,
        window_end=norm_end,
    )
    selected_run_ids = tuple(r.run_id for r in selected_runs)
    selected_run_id_strs = {str(rid) for rid in selected_run_ids}

    all_story_ids: set[UUID] = set()
    all_feed_ids: set[str] = set()
    feed_runs: dict[str, int] = defaultdict(int)
    feed_stories: dict[str, set[UUID]] = defaultdict(set)

    outcome_counts = {"complete": 0, "partial": 0, "empty": 0, "failed": 0}
    for run in selected_runs:
        outcome_counts[run.outcome] += 1
        for feed_id in run.feed_ids:
            all_feed_ids.add(feed_id)
            feed_runs[feed_id] += 1
        for cand in run.candidate_feeds:
            all_story_ids.add(cand.story_id)
            for feed_id in cand.feed_ids:
                all_feed_ids.add(feed_id)
                feed_stories[feed_id].add(cand.story_id)

    decisions: list[EditorialDecision] = []
    if selected_run_id_strs:
        placeholders = ",".join("?" for _ in selected_run_id_strs)
        rows = conn.execute(
            f"SELECT decision_id, story_id, revision, run_id, candidate_sha256, "
            f"decision, reason_code, notes, actor_id, decided_at "
            f"FROM scout_editorial_decisions WHERE run_id IN ({placeholders})",
            tuple(sorted(selected_run_id_strs)),
        ).fetchall()
        decisions = [
            dec for row in rows
            if (dec := _decision_from_row(row)).story_id in all_story_ids
        ]
        decisions.sort(key=lambda d: (d.decided_at, str(d.decision_id)))

    first_assigned: dict[UUID, EditorialDecision] = {}
    passed_stories: set[UUID] = set()
    for dec in decisions:
        if dec.decision == "assigned":
            first_assigned.setdefault(dec.story_id, dec)
        else:
            passed_stories.add(dec.story_id)

    distinct_assigned_ids = set(first_assigned)
    distinct_passed_ids = passed_stories - distinct_assigned_ids
    reviewed_ids = distinct_assigned_ids | distinct_passed_ids
    pending_ids = all_story_ids - reviewed_ids
    assignment_producing_runs = len({dec.run_id for dec in first_assigned.values()})

    total_candidates = len(all_story_ids)
    distinct_assigned = len(distinct_assigned_ids)
    distinct_passed = len(distinct_passed_ids)
    reviewed_stories = len(reviewed_ids)
    pending_stories = len(pending_ids)

    channel_totals = ChannelTotals(
        total_runs=len(selected_runs),
        complete_runs=outcome_counts["complete"],
        partial_runs=outcome_counts["partial"],
        empty_runs=outcome_counts["empty"],
        failed_runs=outcome_counts["failed"],
        assignment_producing_runs=assignment_producing_runs,
        total_candidates=total_candidates,
        distinct_assigned=distinct_assigned,
        distinct_passed=distinct_passed,
        reviewed_stories=reviewed_stories,
        pending_stories=pending_stories,
        review_coverage=(reviewed_stories / total_candidates) if total_candidates > 0 else None,
        assigned_per_reviewed=(distinct_assigned / reviewed_stories) if reviewed_stories > 0 else None,
        assignment_yield=(distinct_assigned / total_candidates) if total_candidates > 0 else None,
        total_decision_events=len(decisions),
    )

    by_feed = tuple(
        FeedYieldSummary(
            feed_id=feed_id,
            runs_participated=feed_runs[feed_id],
            unique_candidates=len(cands := feed_stories[feed_id]),
            reviewed_candidates=len(cands & reviewed_ids),
            pending_candidates=len(cands & pending_ids),
            unique_assignments=len(cands & distinct_assigned_ids),
            assignment_yield=(len(cands & distinct_assigned_ids) / len(cands)) if cands else None,
        )
        for feed_id in sorted(all_feed_ids)
    )

    cost_report = _aggregate_run_costs(selected_runs, ledger_conn=ledger_conn or conn)
    if distinct_assigned > 0:
        per_assignment = pico_to_usd(
            usd_to_pico_ceil(cost_report.total_model_cost_usd / Decimal(distinct_assigned))
        )
        cost_report = cost_report.model_copy(update={"cost_per_distinct_assignment_usd": per_assignment})

    return PilotReport(
        window_start=norm_start,
        window_end=norm_end,
        selected_run_ids=selected_run_ids,
        channel_totals=channel_totals,
        by_feed=by_feed,
        cost=cost_report,
    )


def _check_safe_text(text: str, *, field_name: str) -> None:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise ExportGateError(f"secret_detected_in_export:{field_name}")
    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            raise ExportGateError(f"reviewer_pii_detected_in_export:{field_name}")


def _validate_export_output_path(output_path: Path) -> None:
    resolved = output_path.resolve()
    parts_lower = [part.lower() for part in resolved.parts]
    if "evals" in parts_lower:
        raise ExportGateError("cannot_write_into_frozen_dataset_directory")
    if resolved.parent.exists() and (resolved.parent / "manifest.json").exists():
        raise ExportGateError("cannot_write_into_frozen_dataset_directory")


def export_adjudicated_failures(
    conn: sqlite3.Connection,
    store: ArtifactStore,
    *,
    output_path: Path | None = None,
    rights_cleared: bool,
    privacy_checked: bool,
    dataset_version: str = "next",
    run_ids: Sequence[UUID | str] | None = None,
    decisions_filter: tuple[EditorialDecisionOutcome, ...] = ("passed",),
) -> AdjudicatedFailuresExport:
    """Export rights-cleared, privacy-checked adjudicated editorial failures for future datasets."""
    if rights_cleared is not True:
        raise ExportGateError("rights_clearance_required")
    if privacy_checked is not True:
        raise ExportGateError("privacy_check_required")
    if not dataset_version or not dataset_version.strip():
        raise ExportGateError("dataset_version_required")
    if output_path is not None:
        _validate_export_output_path(output_path)

    query = (
        "SELECT d.decision_id, d.story_id, d.revision, d.run_id, d.candidate_sha256, "
        "d.decision, d.reason_code, d.notes, d.actor_id, d.decided_at, "
        "a.media_type, a.size_bytes, a.schema_version "
        "FROM scout_editorial_decisions d "
        "JOIN artifacts a ON a.sha256 = d.candidate_sha256 "
        "ORDER BY d.decided_at ASC, d.decision_id ASC"
    )
    rows = conn.execute(query).fetchall()
    wanted_runs = {UUID(str(rid)) for rid in run_ids} if run_ids is not None else None

    records: list[AdjudicatedFailureRecord] = []
    for row in rows:
        dec = _decision_from_row(row[:10])
        if dec.decision not in decisions_filter:
            continue
        if wanted_runs is not None and dec.run_id not in wanted_runs:
            continue

        _check_safe_text(dec.reason_code, field_name="reason_code")
        _check_safe_text(dec.notes, field_name="notes")

        ref = ArtifactRef(sha256=dec.candidate_sha256, media_type=row[10], size_bytes=row[11], schema_version=row[12])
        cand = StoryCandidate.model_validate_json(store.read(ref))

        pins: list[ExportedSourcePin] = []
        for member in cand.members:
            src = member.source
            if src.rights == "unknown":
                raise ExportGateError("source_rights_not_cleared")
            if src.rights == "metadata_only":
                if src.excerpt is not None and src.excerpt.strip():
                    raise ExportGateError("metadata_only_excerpt_forbidden")
                safe_excerpt = None
            else:
                if src.excerpt is not None:
                    if len(src.excerpt) > _MAX_EXPORT_EXCERPT_CHARS:
                        raise ExportGateError("copyrighted_body_exceeds_excerpt_limit")
                    _check_safe_text(src.excerpt, field_name="source.excerpt")
                safe_excerpt = src.excerpt
            pins.append(
                ExportedSourcePin(
                    source_id=src.source_id,
                    revision=src.revision,
                    source_record_hash=member.ref.source_record_hash,
                    publisher=src.publisher,
                    source_class=src.source_class,
                    canonical_url=src.canonical_url,
                    rights=src.rights,
                    published_at=src.published_at,
                    retrieved_at=src.retrieved_at,
                    locator=src.locator,
                    excerpt=safe_excerpt,
                )
            )

        records.append(
            AdjudicatedFailureRecord(
                decision_id=dec.decision_id,
                story_id=dec.story_id,
                revision=dec.revision,
                run_id=dec.run_id,
                candidate_sha256=dec.candidate_sha256,
                decision=dec.decision,
                reason_code=dec.reason_code,
                notes=dec.notes,
                decided_at=dec.decided_at,
                topics=cand.topics,
                warnings=cand.warnings,
                source_pins=tuple(pins),
            )
        )

    bundle = AdjudicatedFailuresExport(
        target_dataset_version=dataset_version.strip(),
        exported_at=_utc_now(),
        rights_cleared=True,
        privacy_checked=True,
        record_count=len(records),
        records=tuple(records),
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(bundle.canonical_bytes())
    return bundle
