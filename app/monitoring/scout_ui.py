"""Read-only local News Scout operational monitoring dashboard."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import UUID

from app.monitoring.scout_ops import (
    ScoutOperationalRepository,
    ScoutOperationalSummary,
    ScoutTraceCorrelation,
)


def build_dashboard_view(
    summary: ScoutOperationalSummary,
    correlation: ScoutTraceCorrelation | None = None,
) -> dict[str, object]:
    """Build a sanitized, JSON-serializable operational dashboard view model."""
    view: dict[str, object] = {
        "generated_at": summary.generated_at.isoformat(),
        "run_count": summary.run_count,
        "story_revision_count": summary.story_revision_count,
        "phoenix_outage": summary.telemetry.phoenix_outage,
        "telemetry": summary.telemetry.model_dump(mode="json"),
        "feeds": [feed.model_dump(mode="json") for feed in summary.feeds],
        "item_rates": summary.item_rates.model_dump(mode="json"),
        "source_mix": summary.source_mix.model_dump(mode="json"),
        "duplicate_quality": summary.duplicate_quality.model_dump(mode="json"),
        "latency": summary.latency.model_dump(mode="json"),
        "quarantine": summary.quarantine.model_dump(mode="json"),
        "retries": summary.retries.model_dump(mode="json"),
        "budget": summary.budget.model_dump(mode="json"),
        "handoffs": summary.handoffs.model_dump(mode="json"),
        "alerts": [alert.model_dump(mode="json") for alert in summary.alerts],
        "missing_data": list(summary.missing_data),
    }
    if correlation is not None:
        view["trace_correlation"] = correlation.model_dump(mode="json")
    return view


def render_scout_dashboard(
    repository: ScoutOperationalRepository,
    *,
    since: datetime | None = None,
    probe_collector: bool = False,
) -> None:
    """Render the Streamlit News Scout operational dashboard."""
    import streamlit as st

    st.title("News Scout Operational Monitoring")
    st.caption(
        "Read-only local operational projection. Unknown metrics stay explicit; "
        "no source bodies, credentials, or reviewer notes are displayed."
    )

    summary = repository.snapshot(since=since, probe_collector=probe_collector)

    if summary.telemetry.phoenix_outage:
        st.error(
            "Phoenix collector outage detected: spans are buffered locally in TelemetryOutbox "
            f"(pending={summary.telemetry.pending_spans}, error={summary.telemetry.error_code})."
        )
    for alert in summary.alerts:
        if alert.severity == "critical":
            st.error(f"{alert.code}: {alert.reference} ({alert.delivery} only)")
        else:
            st.warning(f"{alert.code}: {alert.reference} ({alert.delivery} only)")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Runs", summary.run_count)
    col2.metric("Story Revisions", summary.story_revision_count)
    col3.metric(
        "Pending Spans",
        summary.telemetry.pending_spans if summary.telemetry.pending_spans is not None else "unknown",
    )
    col4.metric(
        "Delivered Spans",
        summary.telemetry.delivered_spans if summary.telemetry.delivered_spans is not None else "unknown",
    )

    st.subheader("Feed Freshness & Failure Rates")
    if summary.feeds:
        st.json([feed.model_dump(mode="json") for feed in summary.feeds])
    else:
        st.info("No feeds recorded.")

    st.subheader("Item Rates, Source Mix & Duplicate Quality")
    st.json(
        {
            "item_rates": summary.item_rates.model_dump(mode="json"),
            "source_mix": summary.source_mix.model_dump(mode="json"),
            "duplicate_quality": summary.duplicate_quality.model_dump(mode="json"),
        }
    )

    st.subheader("Latency, Quarantine, Retries, Budget & Handoffs")
    st.json(
        {
            "latency": summary.latency.model_dump(mode="json"),
            "quarantine": summary.quarantine.model_dump(mode="json"),
            "retries": summary.retries.model_dump(mode="json"),
            "budget": summary.budget.model_dump(mode="json"),
            "handoffs": summary.handoffs.model_dump(mode="json"),
        }
    )

    st.subheader("Run / Trace Correlation")
    run_text = st.text_input("Run UUID (optional)")
    trace_text = st.text_input("Trace ID (32-char hex, optional)")
    if run_text or trace_text:
        parsed_run: UUID | None = None
        if run_text.strip():
            try:
                parsed_run = UUID(run_text.strip())
            except ValueError:
                st.warning("Enter a valid run UUID.")
                return
        corr = repository.trace_run(
            run_id=parsed_run,
            trace_id=trace_text.strip() or None,
        )
        st.json(corr.model_dump(mode="json"))


def main() -> None:
    import streamlit as st

    eval_dir = Path(os.environ.get("FINANCE_SCOUT_EVAL_DIR", "data/scout_evals"))
    eval_paths = tuple(sorted(eval_dir.glob("*.json"))) if eval_dir.exists() else ()
    ledger_env = os.environ.get("FINANCE_SCOUT_LEDGER_DATABASE")
    repository = ScoutOperationalRepository(
        Path(os.environ.get("FINANCE_SCOUT_DATABASE", "data/scout.sqlite3")),
        telemetry_path=Path(os.environ.get("FINANCE_SCOUT_TELEMETRY_DATABASE", "data/telemetry.sqlite3")),
        ledger_path=Path(ledger_env) if ledger_env else None,
        eval_report_paths=eval_paths,
    )
    days = st.sidebar.number_input("Window in days (0 = all)", min_value=0, max_value=365, value=7)
    probe = st.sidebar.checkbox("Probe local Phoenix /healthz", value=False)
    since = datetime.now(timezone.utc) - timedelta(days=int(days)) if days > 0 else None
    render_scout_dashboard(repository, since=since, probe_collector=probe)


if __name__ == "__main__":
    main()
