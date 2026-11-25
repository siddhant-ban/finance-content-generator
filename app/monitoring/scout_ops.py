"""Read-only operational monitoring projection and trace correlation for News Scout."""
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError

from app.contracts.common import StrictModel, UtcDatetime
from app.observability.health import probe_phoenix
from app.observability.models import CollectorHealth, SpanRecord
from app.observability.sanitize import sanitize_attributes
from app.scout.ledger import pico_to_usd

SourceClass = Literal["primary", "reputable_secondary", "commentary", "unknown"]
SOURCE_CLASSES: tuple[SourceClass, ...] = ("primary", "reputable_secondary", "commentary", "unknown")


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest rank; empty windows return None."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _parse_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def _safe_uuid(raw: object) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


class ScoutAlert(StrictModel):
    code: Literal[
        "phoenix_outage",
        "telemetry_degraded",
        "feed_failure",
        "quarantine_spike",
        "handoff_failure",
        "budget_ambiguous",
    ]
    reference: str
    severity: Literal["warning", "critical"]
    delivery: Literal["local"] = "local"


class FeedHealthMetric(StrictModel):
    feed_id: str
    latest_revision: int | None = None
    enabled: bool | None = None
    source_class: SourceClass = "unknown"
    last_retrieved_at: UtcDatetime | None = None
    freshness_age_seconds: float | None = None
    poll_count: int = 0
    failure_count: int = 0
    failure_rate: float | None = None
    failure_reasons: dict[str, int] = Field(default_factory=dict)
    status: Literal["measured", "partial", "unknown"]
    availability_reason: str | None = None


class ItemRateMetrics(StrictModel):
    status: Literal["measured", "unknown"]
    total_items: int = 0
    invalid_count: int = 0
    stale_count: int = 0
    withheld_count: int = 0
    accepted_count: int = 0
    invalid_rate: float | None = None
    stale_rate: float | None = None
    availability_reason: str | None = None


class SourceMixMetrics(StrictModel):
    status: Literal["measured", "partial", "unknown"]
    total: int = 0
    counts: dict[SourceClass, int] = Field(
        default_factory=lambda: {cls: 0 for cls in SOURCE_CLASSES}
    )
    shares: dict[SourceClass, float] | None = None
    availability_reason: str | None = None


class DuplicateQualityMetrics(StrictModel):
    status: Literal["measured", "partial", "unknown"]
    precision: float | None = None
    recall: float | None = None
    correct_pairs: int | None = None
    predicted_pairs: int | None = None
    expected_pairs: int | None = None
    report_count: int = 0
    availability_reason: str | None = None


class LatencyMetrics(StrictModel):
    status: Literal["measured", "unknown"]
    sample_count: int = 0
    min_ms: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    max_ms: float | None = None
    mean_ms: float | None = None
    p95_by_operation: dict[str, float | None] = Field(default_factory=dict)
    availability_reason: str | None = None


class QuarantineMetrics(StrictModel):
    status: Literal["measured", "unknown"]
    quarantine_count: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)
    availability_reason: str | None = None


class RetryMetrics(StrictModel):
    status: Literal["measured", "unknown"]
    total_retries: int | None = None
    retried_spans: int | None = None
    availability_reason: str | None = None


class BudgetMetrics(StrictModel):
    status: Literal["measured", "unknown"]
    hard_spend_cap_usd: Decimal | None = None
    reconciled_cost_usd: Decimal | None = None
    pending_reserved_usd: Decimal | None = None
    ambiguous_reserved_usd: Decimal | None = None
    encumbered_usd: Decimal | None = None
    remaining_budget_usd: Decimal | None = None
    pending_count: int | None = None
    reconciled_count: int | None = None
    ambiguous_count: int | None = None
    released_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    availability_reason: str | None = None


class UndeliveredHandoff(StrictModel):
    story_id: UUID
    revision: int
    cluster_id: UUID
    artifact_sha256: str
    run_id: UUID
    invocation_id: UUID
    status: str
    replay_mode: str | None = None
    source_run_id: UUID | None = None
    trace_id: str
    error_code: str | None = None
    updated_at: str


class HandoffMetrics(StrictModel):
    status: Literal["measured", "unknown"]
    pending_count: int | None = None
    failed_count: int | None = None
    undelivered_count: int | None = None
    delivered_count: int | None = None
    error_counts: dict[str, int] = Field(default_factory=dict)
    undelivered_handoffs: tuple[UndeliveredHandoff, ...] = ()
    availability_reason: str | None = None


class TelemetryDeliveryStatus(StrictModel):
    status: Literal["measured", "degraded", "unknown"]
    pending_spans: int | None = None
    delivered_spans: int | None = None
    pending_bytes: int | None = None
    lost_records: int | None = None
    persistence_failures: int | None = None
    export_failures: int | None = None
    error_code: str | None = None
    degraded_runs: tuple[UUID, ...] = ()
    phoenix_outage: bool = False
    collector_available: bool | None = None
    availability_reason: str | None = None


class ScoutRunSummary(StrictModel):
    run_id: UUID
    invocation_id: UUID
    input_sha256: str
    story_revision_count: int = 0
    trace_ids: tuple[str, ...] = ()
    outcome: str | None = None
    release_id: UUID | None = None
    prompt_version: str | None = None
    model_name: str | None = None


class ScoutOperationalSummary(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    generated_at: UtcDatetime
    run_count: int = 0
    story_revision_count: int = 0
    runs: tuple[ScoutRunSummary, ...] = ()
    feeds: tuple[FeedHealthMetric, ...] = ()
    item_rates: ItemRateMetrics
    source_mix: SourceMixMetrics
    duplicate_quality: DuplicateQualityMetrics
    latency: LatencyMetrics
    quarantine: QuarantineMetrics
    retries: RetryMetrics
    budget: BudgetMetrics
    handoffs: HandoffMetrics
    telemetry: TelemetryDeliveryStatus
    alerts: tuple[ScoutAlert, ...] = ()
    missing_data: tuple[str, ...] = ()


class CorrelatedSpan(StrictModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    project: str
    status: str
    start_time_ns: int
    end_time_ns: int
    latency_ms: float
    story_id: UUID | None = None
    attributes: dict[str, object] = Field(default_factory=dict)
    export_status: Literal["pending"] = "pending"


class CorrelatedStoryRevision(StrictModel):
    story_id: UUID
    cluster_id: UUID
    revision: int
    artifact_sha256: str
    reason_code: str
    created_at: str


class ScoutTraceCorrelation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID | None = None
    invocation_id: UUID | None = None
    input_sha256: str | None = None
    trace_id: str | None = None
    telemetry_delivery_status: Literal["pending", "outage_buffered", "delivered", "unknown"]
    spans: tuple[CorrelatedSpan, ...] = ()
    story_revisions: tuple[CorrelatedStoryRevision, ...] = ()
    handoffs: tuple[UndeliveredHandoff, ...] = ()
    missing_data: tuple[str, ...] = ()


class ScoutOperationalRepository:
    """Read-only projection over Scout SQLite, SpendLedger, artifacts, evals, and TelemetryOutbox."""

    def __init__(
        self,
        database: Path,
        *,
        telemetry_path: Path | None = None,
        ledger_path: Path | None = None,
        artifact_root: Path | None = None,
        eval_report_paths: tuple[Path, ...] = (),
        phoenix_probe: Callable[[], CollectorHealth] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.database = database
        self.telemetry_path = telemetry_path
        self.ledger_path = ledger_path
        self.artifact_root = artifact_root if artifact_root is not None else database.parent / "artifacts"
        self.eval_report_paths = eval_report_paths
        self._phoenix_probe = phoenix_probe or probe_phoenix
        self._now = now

    @staticmethod
    @contextmanager
    def connection(path: Path):
        if not path.exists():
            raise sqlite3.OperationalError(f"database missing: {path.name}")
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            conn.close()

    def _read_telemetry(self, *, probe_collector: bool) -> tuple[list[SpanRecord], TelemetryDeliveryStatus, set[str]]:
        missing: set[str] = set()
        collector: CollectorHealth | None = None
        if probe_collector:
            collector = self._phoenix_probe()

        if self.telemetry_path is None:
            missing.add("telemetry_unavailable")
            return [], TelemetryDeliveryStatus(
                status="unknown",
                phoenix_outage=bool(collector is not None and not collector.available),
                collector_available=collector.available if collector is not None else None,
                availability_reason="telemetry_path_not_configured",
            ), missing

        spans: list[SpanRecord] = []
        try:
            with self.connection(self.telemetry_path) as conn:
                if _table_exists(conn, "telemetry_outbox"):
                    for (payload,) in conn.execute("SELECT payload FROM telemetry_outbox ORDER BY id"):
                        try:
                            spans.append(SpanRecord.model_validate_json(payload))
                        except ValueError:
                            missing.add("corrupt_pending_spans")
                elif _table_exists(conn, "spans"):
                    for (payload,) in conn.execute("SELECT payload FROM spans ORDER BY id"):
                        try:
                            spans.append(SpanRecord.model_validate_json(payload))
                        except ValueError:
                            missing.add("corrupt_pending_spans")
                else:
                    missing.add("telemetry_unavailable")
                    return [], TelemetryDeliveryStatus(
                        status="unknown",
                        phoenix_outage=bool(collector is not None and not collector.available),
                        collector_available=collector.available if collector is not None else None,
                        availability_reason="telemetry_table_unavailable",
                    ), missing

                pending_count = len(spans)
                row = conn.execute(
                    "SELECT count(*), coalesce(sum(size_bytes), 0) FROM telemetry_outbox"
                ).fetchone() if _table_exists(conn, "telemetry_outbox") else (pending_count, 0)
                pending_bytes = int(row[1]) if row is not None else 0

                seq = None
                if _table_exists(conn, "sqlite_sequence"):
                    seq_row = conn.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name = 'telemetry_outbox'"
                    ).fetchone()
                    if seq_row is not None:
                        seq = int(seq_row[0])
                total_enqueued = seq if seq is not None else pending_count
                delivered_spans = max(0, total_enqueued - pending_count)

                health_row = None
                if _table_exists(conn, "telemetry_health"):
                    health_row = conn.execute(
                        "SELECT lost_records, persistence_failures, export_failures, error_code "
                        "FROM telemetry_health WHERE singleton = 1"
                    ).fetchone()
                lost_records = int(health_row[0]) if health_row else 0
                persistence_failures = int(health_row[1]) if health_row else 0
                export_failures = int(health_row[2]) if health_row else 0
                error_code = health_row[3] if health_row else None

                degraded_runs: list[UUID] = []
                if _table_exists(conn, "telemetry_degraded_runs"):
                    for (raw_run,) in conn.execute("SELECT run_id FROM telemetry_degraded_runs ORDER BY run_id"):
                        parsed = _safe_uuid(raw_run)
                        if parsed is not None:
                            degraded_runs.append(parsed)
        except sqlite3.Error:
            missing.add("telemetry_unavailable")
            return [], TelemetryDeliveryStatus(
                status="unknown",
                phoenix_outage=bool(collector is not None and not collector.available),
                collector_available=collector.available if collector is not None else None,
                availability_reason="telemetry_unavailable",
            ), missing

        outage = bool(
            error_code in {"export_failed", "export_timeout"}
            or (collector is not None and not collector.available)
            or (pending_count > 0 and export_failures > 0 and (collector is None or not collector.available))
        )
        degraded = bool(outage or lost_records or persistence_failures or error_code or degraded_runs)
        status: Literal["measured", "degraded", "unknown"] = "degraded" if degraded else "measured"
        return spans, TelemetryDeliveryStatus(
            status=status,
            pending_spans=pending_count,
            delivered_spans=delivered_spans,
            pending_bytes=pending_bytes,
            lost_records=lost_records,
            persistence_failures=persistence_failures,
            export_failures=export_failures,
            error_code=error_code,
            degraded_runs=tuple(degraded_runs),
            phoenix_outage=outage,
            collector_available=collector.available if collector is not None else None,
            availability_reason="phoenix_outage" if outage else None,
        ), missing

    def _read_artifact_json(self, sha256: str) -> dict | None:
        if not isinstance(sha256, str) or len(sha256) != 64:
            return None
        path = self.artifact_root / sha256[:2] / sha256
        try:
            if path.is_symlink() or not path.exists():
                return None
            return json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None

    def _read_feeds(
        self,
        conn: sqlite3.Connection | None,
        fetch_spans: list[SpanRecord],
        now_dt: datetime,
    ) -> tuple[FeedHealthMetric, ...]:
        revisions: dict[str, dict] = {}
        caches: dict[tuple[str, int], datetime] = {}

        if conn is not None:
            if _table_exists(conn, "scout_feed_revisions"):
                for feed_id, revision, body in conn.execute(
                    "SELECT feed_id, revision, body FROM scout_feed_revisions ORDER BY feed_id, revision"
                ):
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        continue
                    revisions[feed_id] = {
                        "revision": int(revision),
                        "enabled": parsed.get("enabled"),
                        "source_class": parsed.get("source_class", "unknown"),
                    }
            if _table_exists(conn, "scout_feed_cache"):
                for feed_id, revision, body in conn.execute(
                    "SELECT feed_id, revision, body FROM scout_feed_cache"
                ):
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        continue
                    retrieved = _parse_utc(parsed.get("retrieved_at"))
                    if retrieved is not None:
                        caches[(feed_id, int(revision))] = retrieved

        polls_by_feed: dict[str, list[SpanRecord]] = {}
        for span in fetch_spans:
            feed_id = span.attributes.get("feed.id")
            if isinstance(feed_id, str):
                polls_by_feed.setdefault(feed_id, []).append(span)

        all_feed_ids = sorted(set(revisions) | set(polls_by_feed))
        results: list[FeedHealthMetric] = []
        for feed_id in all_feed_ids:
            rev_info = revisions.get(feed_id, {})
            rev_num = rev_info.get("revision")
            feed_spans = polls_by_feed.get(feed_id, [])

            last_retrieved = caches.get((feed_id, rev_num)) if rev_num is not None else None
            for span in feed_spans:
                if span.attributes.get("feed.status") in {"read", "not_modified"}:
                    span_dt = datetime.fromtimestamp(span.end_time_ns / 1_000_000_000, tz=timezone.utc)
                    if last_retrieved is None or span_dt > last_retrieved:
                        last_retrieved = span_dt

            age_seconds = (
                max(0.0, (now_dt - last_retrieved).total_seconds())
                if last_retrieved is not None
                else None
            )
            poll_count = len(feed_spans)
            failure_reasons: Counter[str] = Counter()
            failure_count = 0
            for span in feed_spans:
                f_status = span.attributes.get("feed.status")
                if f_status in {"unavailable", "limited"}:
                    failure_count += 1
                    reason = span.attributes.get("feed.reason")
                    if isinstance(reason, str):
                        failure_reasons[reason] += 1
                    else:
                        failure_reasons[str(f_status)] += 1

            failure_rate = (failure_count / poll_count) if poll_count > 0 else None
            if poll_count > 0 and last_retrieved is not None:
                status: Literal["measured", "partial", "unknown"] = "measured"
                reason_str = None
            elif poll_count > 0 or last_retrieved is not None:
                status = "partial"
                reason_str = "no_poll_telemetry" if poll_count == 0 else "never_retrieved"
            else:
                status = "unknown"
                reason_str = "no_poll_telemetry"

            src_cls = rev_info.get("source_class", "unknown")
            if src_cls not in SOURCE_CLASSES:
                src_cls = "unknown"

            results.append(
                FeedHealthMetric(
                    feed_id=feed_id,
                    latest_revision=rev_num,
                    enabled=rev_info.get("enabled"),
                    source_class=src_cls,
                    last_retrieved_at=last_retrieved,
                    freshness_age_seconds=age_seconds,
                    poll_count=poll_count,
                    failure_count=failure_count,
                    failure_rate=failure_rate,
                    failure_reasons=dict(sorted(failure_reasons.items())),
                    status=status,
                    availability_reason=reason_str,
                )
            )
        return tuple(results)

    def _read_duplicate_quality(self) -> tuple[DuplicateQualityMetrics, set[str]]:
        missing: set[str] = set()
        if not self.eval_report_paths:
            missing.add("duplicate_quality_unlabeled")
            return DuplicateQualityMetrics(
                status="unknown",
                availability_reason="unlabeled_production_traffic",
            ), missing

        correct_total = 0
        predicted_total = 0
        expected_total = 0
        precisions: list[float] = []
        recalls: list[float] = []
        valid_reports = 0
        saw_pairs = False

        for path in self.eval_report_paths:
            try:
                data = json.loads(path.read_bytes())
            except (OSError, ValueError):
                missing.add("eval_report_invalid")
                continue
            if not isinstance(data, dict):
                missing.add("eval_report_invalid")
                continue
            metrics = data.get("metrics")
            pairs = data.get("duplicate_pairs")
            if not isinstance(metrics, dict):
                missing.add("eval_report_invalid")
                continue
            p_val = metrics.get("duplicate_precision")
            r_val = metrics.get("duplicate_recall")
            if isinstance(p_val, (int, float)) and not isinstance(p_val, bool):
                precisions.append(float(p_val))
            if isinstance(r_val, (int, float)) and not isinstance(r_val, bool):
                recalls.append(float(r_val))
            if isinstance(pairs, dict) and all(
                isinstance(pairs.get(k), int) and not isinstance(pairs.get(k), bool)
                for k in ("correct", "predicted", "expected")
            ):
                saw_pairs = True
                correct_total += int(pairs["correct"])
                predicted_total += int(pairs["predicted"])
                expected_total += int(pairs["expected"])
            valid_reports += 1

        if valid_reports == 0:
            return DuplicateQualityMetrics(
                status="unknown",
                availability_reason="eval_evidence_unavailable",
            ), missing

        precision = (
            (correct_total / predicted_total)
            if saw_pairs and predicted_total > 0
            else (sum(precisions) / len(precisions) if precisions else None)
        )
        recall = (
            (correct_total / expected_total)
            if saw_pairs and expected_total > 0
            else (sum(recalls) / len(recalls) if recalls else None)
        )
        if precision is not None and recall is not None:
            status: Literal["measured", "partial", "unknown"] = "measured"
            avail = None
        elif precision is not None or recall is not None:
            status = "partial"
            avail = "partial_pair_coverage"
        else:
            status = "unknown"
            avail = "unlabeled_production_traffic"

        return DuplicateQualityMetrics(
            status=status,
            precision=precision,
            recall=recall,
            correct_pairs=correct_total if saw_pairs else None,
            predicted_pairs=predicted_total if saw_pairs else None,
            expected_pairs=expected_total if saw_pairs else None,
            report_count=valid_reports,
            availability_reason=avail,
        ), missing

    def _read_budget(self, scout_conn: sqlite3.Connection | None) -> tuple[BudgetMetrics, Counter[str], set[str]]:
        missing: set[str] = set()
        quarantine_reasons: Counter[str] = Counter()

        target_conn = None
        owns_conn = False
        try:
            if self.ledger_path is not None:
                try:
                    with self.connection(self.ledger_path) as lconn:
                        return self._extract_budget_from_conn(lconn, missing, quarantine_reasons)
                except sqlite3.Error:
                    missing.add("ledger_unavailable")
                    return BudgetMetrics(status="unknown", availability_reason="ledger_unavailable"), quarantine_reasons, missing
            target_conn = scout_conn
            if target_conn is None or not _table_exists(target_conn, "scout_eval_reservations"):
                missing.add("ledger_unavailable")
                return BudgetMetrics(status="unknown", availability_reason="ledger_unavailable"), quarantine_reasons, missing
            return self._extract_budget_from_conn(target_conn, missing, quarantine_reasons)
        finally:
            if owns_conn and target_conn is not None:
                target_conn.close()

    @staticmethod
    def _extract_budget_from_conn(
        conn: sqlite3.Connection,
        missing: set[str],
        quarantine_reasons: Counter[str],
    ) -> tuple[BudgetMetrics, Counter[str], set[str]]:
        if not _table_exists(conn, "scout_eval_reservations"):
            missing.add("ledger_unavailable")
            return BudgetMetrics(status="unknown", availability_reason="ledger_unavailable"), quarantine_reasons, missing

        cap_pico: int | None = None
        if _table_exists(conn, "scout_eval_experiments"):
            cap_row = conn.execute("SELECT COALESCE(SUM(hard_spend_cap_pico), 0), COUNT(*) FROM scout_eval_experiments").fetchone()
            if cap_row and cap_row[1] > 0:
                cap_pico = int(cap_row[0])

        agg = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status = 'reconciled' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status = 'ambiguous' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status = 'released' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status = 'pending' THEN reserved_pico ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status = 'ambiguous' THEN reserved_pico ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status = 'reconciled' THEN COALESCE(charged_pico, 0) ELSE 0 END), 0), "
            "SUM(input_tokens), "
            "SUM(output_tokens) "
            "FROM scout_eval_reservations"
        ).fetchone()
        for (disp,) in conn.execute(
            "SELECT failure_disposition FROM scout_eval_reservations WHERE failure_disposition IS NOT NULL"
        ):
            if isinstance(disp, str) and disp.strip():
                quarantine_reasons[disp] += 1

        (
            pending_cnt,
            reconciled_cnt,
            ambiguous_cnt,
            released_cnt,
            pending_pico,
            ambiguous_pico,
            reconciled_pico,
            in_tok,
            out_tok,
        ) = agg
        encumbered_pico = int(pending_pico) + int(ambiguous_pico) + int(reconciled_pico)
        remaining_usd = pico_to_usd(max(0, cap_pico - encumbered_pico)) if cap_pico is not None else None
        return BudgetMetrics(
            status="measured",
            hard_spend_cap_usd=pico_to_usd(cap_pico) if cap_pico is not None else None,
            reconciled_cost_usd=pico_to_usd(int(reconciled_pico)),
            pending_reserved_usd=pico_to_usd(int(pending_pico)),
            ambiguous_reserved_usd=pico_to_usd(int(ambiguous_pico)),
            encumbered_usd=pico_to_usd(encumbered_pico),
            remaining_budget_usd=remaining_usd,
            pending_count=int(pending_cnt),
            reconciled_count=int(reconciled_cnt),
            ambiguous_count=int(ambiguous_cnt),
            released_count=int(released_cnt),
            input_tokens=int(in_tok) if in_tok is not None else None,
            output_tokens=int(out_tok) if out_tok is not None else None,
        ), quarantine_reasons, missing

    @staticmethod
    def _read_handoffs(conn: sqlite3.Connection | None) -> tuple[HandoffMetrics, set[str]]:
        missing: set[str] = set()
        if conn is None or not _table_exists(conn, "scout_deliveries"):
            missing.add("scout_deliveries_table_unavailable")
            return HandoffMetrics(
                status="unknown",
                availability_reason="scout_deliveries_table_unavailable",
            ), missing

        pending = 0
        failed = 0
        delivered = 0
        errors: Counter[str] = Counter()
        undelivered: list[UndeliveredHandoff] = []
        try:
            rows = conn.execute(
                "SELECT story_id, revision, cluster_id, artifact_sha256, run_id, invocation_id, "
                "status, replay_mode, source_run_id, trace_id, error_code, created_at, updated_at "
                "FROM scout_deliveries ORDER BY updated_at DESC"
            ).fetchall()
        except sqlite3.Error:
            missing.add("scout_deliveries_schema_incompatible")
            return HandoffMetrics(
                status="unknown",
                availability_reason="scout_deliveries_schema_incompatible",
            ), missing
        for row in rows:
            (
                story_id,
                revision,
                cluster_id,
                artifact_sha256,
                run_id,
                invocation_id,
                status,
                replay_mode,
                source_run_id,
                trace_id,
                error_code,
                _created_at,
                updated_at,
            ) = row
            if error_code:
                errors[str(error_code)] += 1
            if status == "pending":
                pending += 1
            elif status == "failed":
                failed += 1
            else:
                delivered += 1
            if status in {"pending", "failed"}:
                s_uuid = _safe_uuid(story_id)
                c_uuid = _safe_uuid(cluster_id)
                r_uuid = _safe_uuid(run_id)
                i_uuid = _safe_uuid(invocation_id)
                if s_uuid and c_uuid and r_uuid and i_uuid:
                    undelivered.append(
                        UndeliveredHandoff(
                            story_id=s_uuid,
                            revision=int(revision),
                            cluster_id=c_uuid,
                            artifact_sha256=str(artifact_sha256),
                            run_id=r_uuid,
                            invocation_id=i_uuid,
                            status=str(status),
                            replay_mode=str(replay_mode) if replay_mode is not None else None,
                            source_run_id=_safe_uuid(source_run_id),
                            trace_id=str(trace_id),
                            error_code=str(error_code) if error_code is not None else None,
                            updated_at=str(updated_at),
                        )
                    )
        return HandoffMetrics(
            status="measured",
            pending_count=pending,
            failed_count=failed,
            undelivered_count=pending + failed,
            delivered_count=delivered,
            error_counts=dict(sorted(errors.items())),
            undelivered_handoffs=tuple(undelivered),
        ), missing

    def snapshot(self, since: datetime | None = None, *, probe_collector: bool = False) -> ScoutOperationalSummary:
        now_dt = self._now()
        if since is not None and (since.tzinfo is None or since.utcoffset() is None):
            raise ValueError("since must be timezone-aware")
        since_ns = int(since.timestamp() * 1_000_000_000) if since is not None else None

        spans, telemetry_status, missing = self._read_telemetry(probe_collector=probe_collector)
        filtered_spans = [
            s for s in spans
            if since_ns is None or s.start_time_ns >= since_ns
        ]

        scout_conn: sqlite3.Connection | None = None
        try:
            with self.connection(self.database) as conn:
                scout_conn = conn
                return self._build_summary(
                    conn=conn,
                    spans=filtered_spans,
                    telemetry_status=telemetry_status,
                    missing=missing,
                    now_dt=now_dt,
                )
        except sqlite3.Error:
            missing.add("scout_database_unavailable")
            return self._build_summary(
                conn=None,
                spans=filtered_spans,
                telemetry_status=telemetry_status,
                missing=missing,
                now_dt=now_dt,
            )

    def _build_summary(
        self,
        *,
        conn: sqlite3.Connection | None,
        spans: list[SpanRecord],
        telemetry_status: TelemetryDeliveryStatus,
        missing: set[str],
        now_dt: datetime,
    ) -> ScoutOperationalSummary:
        fetch_spans = [s for s in spans if s.name == "scout.fetch"]
        item_spans = [s for s in spans if s.name == "scout.item"]

        feeds = self._read_feeds(conn, fetch_spans, now_dt)

        # Item rates & item-level quarantines
        telemetry_available = telemetry_status.status != "unknown"
        item_quarantines: Counter[str] = Counter()
        if item_spans:
            invalid_cnt = 0
            stale_cnt = 0
            withheld_cnt = 0
            accepted_cnt = 0
            for span in item_spans:
                i_status = span.attributes.get("item.status")
                i_reason = span.attributes.get("item.reason")
                if i_status in {"new", "updated"}:
                    accepted_cnt += 1
                elif i_status == "withheld":
                    withheld_cnt += 1
                if i_reason == "outside_window":
                    stale_cnt += 1
                if i_status == "failed" or i_reason in {"invalid_provenance", "malformed_feed_item", "invalid_enrichment"}:
                    invalid_cnt += 1
                    if isinstance(i_reason, str):
                        item_quarantines[i_reason] += 1
                elif i_reason == "outside_topic_policy":
                    item_quarantines[str(i_reason)] += 1
            total_items = len(item_spans)
            item_rates = ItemRateMetrics(
                status="measured",
                total_items=total_items,
                invalid_count=invalid_cnt,
                stale_count=stale_cnt,
                withheld_count=withheld_cnt,
                accepted_count=accepted_cnt,
                invalid_rate=invalid_cnt / total_items,
                stale_rate=stale_cnt / total_items,
            )
        else:
            item_rates = ItemRateMetrics(
                status="unknown",
                availability_reason="telemetry_unavailable" if not telemetry_available else "no_item_telemetry",
            )

        # Source mix (from scout.item spans, falling back to persisted StoryCandidate artifacts)
        mix_counts: dict[SourceClass, int] = {cls: 0 for cls in SOURCE_CLASSES}
        if item_spans:
            for span in item_spans:
                cls = span.attributes.get("source.class")
                key: SourceClass = cls if cls in SOURCE_CLASSES else "unknown"  # type: ignore[assignment]
                mix_counts[key] += 1
            total_mix = sum(mix_counts.values())
            source_mix = SourceMixMetrics(
                status="measured",
                total=total_mix,
                counts=mix_counts,
                shares={k: v / total_mix for k, v in mix_counts.items()} if total_mix > 0 else None,
            )
        else:
            candidate_mix = self._source_mix_from_candidates(conn)
            if candidate_mix is not None:
                total_mix = sum(candidate_mix.values())
                source_mix = SourceMixMetrics(
                    status="partial",
                    total=total_mix,
                    counts=candidate_mix,
                    shares={k: v / total_mix for k, v in candidate_mix.items()} if total_mix > 0 else None,
                    availability_reason="accepted_candidates_only",
                )
            else:
                source_mix = SourceMixMetrics(
                    status="unknown",
                    availability_reason="telemetry_unavailable" if not telemetry_available else "no_source_evidence",
                )

        # Latency & retries
        latencies: list[float] = []
        latencies_by_op: dict[str, list[float]] = {}
        for span in spans:
            raw_lat = span.attributes.get("latency_ms")
            lat = float(raw_lat) if isinstance(raw_lat, (int, float)) else (span.end_time_ns - span.start_time_ns) / 1_000_000
            latencies.append(lat)
            latencies_by_op.setdefault(span.name, []).append(lat)

        if latencies:
            latency = LatencyMetrics(
                status="measured",
                sample_count=len(latencies),
                min_ms=min(latencies),
                p50_ms=_percentile(latencies, 0.50),
                p95_ms=_percentile(latencies, 0.95),
                max_ms=max(latencies),
                mean_ms=sum(latencies) / len(latencies),
                p95_by_operation={op: _percentile(vals, 0.95) for op, vals in sorted(latencies_by_op.items())},
            )
        else:
            latency = LatencyMetrics(
                status="unknown",
                availability_reason="telemetry_unavailable" if not telemetry_available else "no_span_telemetry",
            )

        if spans:
            retry_total = 0
            retried_spans = 0
            for span in spans:
                r_cnt = span.attributes.get("retry.count")
                if isinstance(r_cnt, int) and r_cnt > 0:
                    retry_total += r_cnt
                    retried_spans += 1
            retries = RetryMetrics(status="measured", total_retries=retry_total, retried_spans=retried_spans)
        else:
            retries = RetryMetrics(
                status="unknown",
                availability_reason="telemetry_unavailable" if not telemetry_available else "no_span_telemetry",
            )

        dup_quality, dup_missing = self._read_duplicate_quality()
        missing |= dup_missing

        budget, ledger_quarantines, budget_missing = self._read_budget(conn)
        missing |= budget_missing

        handoffs, handoff_missing = self._read_handoffs(conn)
        missing |= handoff_missing

        # Combine quarantine reasons from ledger & item spans (and handoff failures)
        combined_quarantine = Counter(ledger_quarantines)
        for reason_key, cnt in item_quarantines.items():
            # Avoid double-counting when ledger already recorded a specific enrichment disposition for the same failure.
            if reason_key == "invalid_enrichment" and ledger_quarantines:
                continue
            combined_quarantine[reason_key] += cnt

        if telemetry_available or budget.status == "measured":
            quarantine = QuarantineMetrics(
                status="measured",
                quarantine_count=sum(combined_quarantine.values()),
                reasons=dict(sorted(combined_quarantine.items())),
            )
        else:
            quarantine = QuarantineMetrics(
                status="unknown",
                availability_reason="telemetry_and_ledger_unavailable",
            )

        runs, story_rev_count = self._read_runs(conn, spans)

        alerts: list[ScoutAlert] = []
        if telemetry_status.phoenix_outage:
            alerts.append(
                ScoutAlert(code="phoenix_outage", reference="telemetry:phoenix_outage", severity="critical")
            )
        elif telemetry_status.status == "degraded":
            alerts.append(
                ScoutAlert(code="telemetry_degraded", reference="telemetry:outbox", severity="warning")
            )
        for feed in feeds:
            if feed.failure_count > 0:
                alerts.append(
                    ScoutAlert(code="feed_failure", reference=f"feed:{feed.feed_id}", severity="warning")
                )
        if budget.ambiguous_count and budget.ambiguous_count > 0:
            alerts.append(
                ScoutAlert(code="budget_ambiguous", reference="ledger:ambiguous", severity="warning")
            )
        if handoffs.failed_count and handoffs.failed_count > 0:
            alerts.append(
                ScoutAlert(code="handoff_failure", reference="scout_deliveries:failed", severity="critical")
            )

        return ScoutOperationalSummary(
            generated_at=now_dt,
            run_count=len(runs),
            story_revision_count=story_rev_count,
            runs=runs,
            feeds=feeds,
            item_rates=item_rates,
            source_mix=source_mix,
            duplicate_quality=dup_quality,
            latency=latency,
            quarantine=quarantine,
            retries=retries,
            budget=budget,
            handoffs=handoffs,
            telemetry=telemetry_status,
            alerts=tuple(alerts),
            missing_data=tuple(sorted(missing)),
        )

    def _source_mix_from_candidates(self, conn: sqlite3.Connection | None) -> dict[SourceClass, int] | None:
        if conn is None or not _table_exists(conn, "scout_story_revisions"):
            return None
        rows = conn.execute("SELECT artifact_sha256 FROM scout_story_revisions").fetchall()
        if not rows:
            return None
        counts: dict[SourceClass, int] = {cls: 0 for cls in SOURCE_CLASSES}
        loaded = 0
        for (sha,) in rows:
            data = self._read_artifact_json(sha)
            if not isinstance(data, dict):
                continue
            members = data.get("members")
            if not isinstance(members, list):
                continue
            loaded += 1
            for member in members:
                src = member.get("source") if isinstance(member, dict) else None
                cls = src.get("source_class") if isinstance(src, dict) else None
                key: SourceClass = cls if cls in SOURCE_CLASSES else "unknown"  # type: ignore[assignment]
                counts[key] += 1
        return counts if loaded > 0 else None

    def _read_runs(
        self, conn: sqlite3.Connection | None, spans: list[SpanRecord]
    ) -> tuple[tuple[ScoutRunSummary, ...], int]:
        runs_by_id: dict[UUID, dict] = {}
        story_rev_total = 0

        if conn is not None and _table_exists(conn, "scout_run_requests"):
            for run_id_raw, inv_raw, sha in conn.execute(
                "SELECT run_id, invocation_id, input_sha256 FROM scout_run_requests"
            ):
                r_uuid = _safe_uuid(run_id_raw)
                i_uuid = _safe_uuid(inv_raw)
                if r_uuid is None or i_uuid is None:
                    continue
                req_json = self._read_artifact_json(sha)
                cfg = req_json.get("configuration") if isinstance(req_json, dict) else None
                runs_by_id[r_uuid] = {
                    "run_id": r_uuid,
                    "invocation_id": i_uuid,
                    "input_sha256": str(sha),
                    "story_revision_count": 0,
                    "trace_ids": set(),
                    "outcome": None,
                    "release_id": _safe_uuid(req_json.get("release_id")) if isinstance(req_json, dict) else None,
                    "prompt_version": cfg.get("prompt_version") if isinstance(cfg, dict) else None,
                    "model_name": cfg.get("model") if isinstance(cfg, dict) else None,
                }

        if conn is not None and _table_exists(conn, "scout_story_revisions"):
            for (run_id_raw,) in conn.execute("SELECT run_id FROM scout_story_revisions"):
                story_rev_total += 1
                r_uuid = _safe_uuid(run_id_raw)
                if r_uuid in runs_by_id:
                    runs_by_id[r_uuid]["story_revision_count"] += 1

        for span in spans:
            if span.run_id is None:
                continue
            entry = runs_by_id.get(span.run_id)
            if entry is None:
                continue
            entry["trace_ids"].add(span.trace_id)
            if span.name in {"agent.invoke", "scout.run"}:
                outcome = span.attributes.get("run.outcome")
                if isinstance(outcome, str):
                    entry["outcome"] = outcome

        summaries = tuple(
            ScoutRunSummary(
                run_id=item["run_id"],
                invocation_id=item["invocation_id"],
                input_sha256=item["input_sha256"],
                story_revision_count=item["story_revision_count"],
                trace_ids=tuple(sorted(item["trace_ids"])),
                outcome=item["outcome"],
                release_id=item["release_id"],
                prompt_version=item["prompt_version"],
                model_name=item["model_name"],
            )
            for item in sorted(runs_by_id.values(), key=lambda x: str(x["run_id"]))
        )
        return summaries, story_rev_total

    def trace_run(self, run_id: UUID | None = None, *, trace_id: str | None = None) -> ScoutTraceCorrelation:
        """Correlate a Scout run or trace_id across run requests, story revisions, deliveries, and spans."""
        if run_id is None and trace_id is None:
            raise ValueError("run_id or trace_id is required")

        spans, telemetry_status, missing = self._read_telemetry(probe_collector=False)
        matched_spans = [
            s for s in spans
            if (run_id is None or s.run_id == run_id)
            and (trace_id is None or s.trace_id == trace_id)
        ]
        resolved_run_id = run_id
        resolved_trace_id = trace_id
        if resolved_run_id is None:
            for s in matched_spans:
                if s.run_id is not None:
                    resolved_run_id = s.run_id
                    break
        if resolved_trace_id is None and matched_spans:
            resolved_trace_id = matched_spans[0].trace_id

        invocation_id: UUID | None = None
        input_sha256: str | None = None
        story_revs: list[CorrelatedStoryRevision] = []
        handoffs: list[UndeliveredHandoff] = []

        try:
            with self.connection(self.database) as conn:
                if resolved_run_id is None and resolved_trace_id is not None and _table_exists(conn, "scout_deliveries"):
                    try:
                        d_row = conn.execute(
                            "SELECT run_id FROM scout_deliveries WHERE trace_id = ? LIMIT 1",
                            (resolved_trace_id,),
                        ).fetchone()
                        if d_row:
                            resolved_run_id = _safe_uuid(d_row[0])
                    except sqlite3.Error:
                        missing.add("scout_deliveries_schema_incompatible")

                if resolved_run_id is not None and _table_exists(conn, "scout_run_requests"):
                    req_row = conn.execute(
                        "SELECT invocation_id, input_sha256 FROM scout_run_requests WHERE run_id = ?",
                        (str(resolved_run_id),),
                    ).fetchone()
                    if req_row:
                        invocation_id = _safe_uuid(req_row[0])
                        input_sha256 = str(req_row[1])

                if resolved_run_id is not None and _table_exists(conn, "scout_story_revisions"):
                    for row in conn.execute(
                        "SELECT story_id, cluster_id, revision, artifact_sha256, reason_code, created_at "
                        "FROM scout_story_revisions WHERE run_id = ? ORDER BY created_at, revision",
                        (str(resolved_run_id),),
                    ):
                        s_uuid = _safe_uuid(row[0])
                        c_uuid = _safe_uuid(row[1])
                        if s_uuid and c_uuid:
                            story_revs.append(
                                CorrelatedStoryRevision(
                                    story_id=s_uuid,
                                    cluster_id=c_uuid,
                                    revision=int(row[2]),
                                    artifact_sha256=str(row[3]),
                                    reason_code=str(row[4]),
                                    created_at=str(row[5]),
                                )
                            )

                if _table_exists(conn, "scout_deliveries"):
                    where = "run_id = ?" if resolved_run_id is not None else "trace_id = ?"
                    param = str(resolved_run_id) if resolved_run_id is not None else str(resolved_trace_id)
                    try:
                        for row in conn.execute(
                            f"SELECT story_id, revision, cluster_id, artifact_sha256, run_id, invocation_id, "
                            f"status, replay_mode, source_run_id, trace_id, error_code, updated_at "
                            f"FROM scout_deliveries WHERE {where}",
                            (param,),
                        ):
                            s_uuid = _safe_uuid(row[0])
                            c_uuid = _safe_uuid(row[2])
                            r_uuid = _safe_uuid(row[4])
                            i_uuid = _safe_uuid(row[5])
                            if s_uuid and c_uuid and r_uuid and i_uuid:
                                handoffs.append(
                                    UndeliveredHandoff(
                                        story_id=s_uuid,
                                        revision=int(row[1]),
                                        cluster_id=c_uuid,
                                        artifact_sha256=str(row[3]),
                                        run_id=r_uuid,
                                        invocation_id=i_uuid,
                                        status=str(row[6]),
                                        replay_mode=str(row[7]) if row[7] is not None else None,
                                        source_run_id=_safe_uuid(row[8]),
                                        trace_id=str(row[9]),
                                        error_code=str(row[10]) if row[10] is not None else None,
                                        updated_at=str(row[11]),
                                    )
                                )
                    except sqlite3.Error:
                        missing.add("scout_deliveries_schema_incompatible")
        except sqlite3.Error:
            missing.add("scout_database_unavailable")

        # Include any spans sharing resolved_trace_id if initial lookup was by run_id
        if resolved_trace_id is not None and run_id is not None:
            matched_spans = [s for s in spans if s.run_id == run_id or s.trace_id == resolved_trace_id]

        correlated_spans = tuple(
            CorrelatedSpan(
                trace_id=s.trace_id,
                span_id=s.span_id,
                parent_span_id=s.parent_span_id,
                name=s.name,
                project=s.project,
                status=s.status,
                start_time_ns=s.start_time_ns,
                end_time_ns=s.end_time_ns,
                latency_ms=float(s.attributes.get("latency_ms"))
                if isinstance(s.attributes.get("latency_ms"), (int, float))
                else (s.end_time_ns - s.start_time_ns) / 1_000_000,
                story_id=s.story_id,
                attributes=sanitize_attributes(s.attributes),
            )
            for s in sorted(matched_spans, key=lambda item: (item.start_time_ns, item.span_id))
        )

        if telemetry_status.status == "unknown":
            delivery_state: Literal["pending", "outage_buffered", "delivered", "unknown"] = "unknown"
        elif correlated_spans:
            delivery_state = "outage_buffered" if telemetry_status.phoenix_outage else "pending"
        elif telemetry_status.delivered_spans and telemetry_status.delivered_spans > 0:
            delivery_state = "delivered"
        else:
            delivery_state = "unknown"

        return ScoutTraceCorrelation(
            run_id=resolved_run_id,
            invocation_id=invocation_id,
            input_sha256=input_sha256,
            trace_id=resolved_trace_id,
            telemetry_delivery_status=delivery_state,
            spans=correlated_spans,
            story_revisions=tuple(story_revs),
            handoffs=tuple(handoffs),
            missing_data=tuple(sorted(missing)),
        )
