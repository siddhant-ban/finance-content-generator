"""Read-only local inspection for Scout editorial assignments and pilot cost reports."""
from datetime import datetime
import os
from pathlib import Path
import sqlite3

from app.scout.editorial import generate_pilot_report, list_candidates
from app.storage.artifacts import ArtifactStore

BOUNDARY_NOTICE = (
    "Editorial assignment labels ('assigned', 'passed', 'pending') track Scout discovery triage only "
    "and never modify publication approval (AWAITING_APPROVAL / APPROVED_FOR_PUBLISH) "
    "or release approval (ReleaseCandidate)."
)


def _safe_text(value: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in value)


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def editorial_overview(
    db_path: Path,
    *,
    ledger_db_path: Path | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict:
    """Return read-only candidate editorial statuses and pilot yield/cost metrics."""
    conn = _open_readonly(db_path)
    ledger_conn = _open_readonly(ledger_db_path) if ledger_db_path is not None else None
    try:
        report = generate_pilot_report(
            conn,
            ledger_conn=ledger_conn,
            window_start=window_start,
            window_end=window_end,
        )
        artifacts_dir = db_path.parent / "artifacts"
        candidates = []
        if artifacts_dir.is_dir():
            store = ArtifactStore(artifacts_dir, conn)
            for item in list_candidates(conn, store):
                dumped = item.model_dump(mode="json")
                dumped["headline"] = _safe_text(dumped["headline"])
                dumped["publisher"] = _safe_text(dumped["publisher"])
                candidates.append(dumped)
        return {
            "boundary_notice": BOUNDARY_NOTICE,
            "candidates": candidates,
            "report": report.model_dump(mode="json"),
        }
    finally:
        if ledger_conn is not None:
            ledger_conn.close()
        conn.close()


def main() -> None:
    import streamlit as st

    st.title("Scout Editorial Assignments & Pilot Cost")
    st.info(BOUNDARY_NOTICE)
    db_path = Path(os.environ.get("FINANCE_SCOUT_DATABASE", "data/scout/scout.sqlite3"))
    ledger_env = os.environ.get("FINANCE_SCOUT_LEDGER_DATABASE")
    ledger_path = Path(ledger_env) if ledger_env else None
    data = editorial_overview(db_path, ledger_db_path=ledger_path)

    st.subheader("Pilot Report")
    st.json(data["report"])
    st.subheader("Discovered Candidates")
    for cand in data["candidates"]:
        st.write(f"{cand['status'].upper()} | r{cand['revision']} | {cand['headline']} ({cand['publisher']})")
    st.caption("Record decisions via CLI: python -m app.scout.editorial_cli decide ...")


if __name__ == "__main__":
    main()
