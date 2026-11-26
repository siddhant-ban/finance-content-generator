"""Standalone CLI for Scout editorial assignment inspection, decision recording, pilot reporting, and gated export."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sys
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from app.contracts.scout import ScoutRunOutput
from app.scout.editorial import (
    EditorialDecisionError,
    ExportGateError,
    RunCostAttempt,
    export_adjudicated_failures,
    generate_pilot_report,
    list_candidates,
    record_decision,
    record_pilot_run,
)
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database

_COST_RECORDS_ADAPTER = TypeAdapter(tuple[RunCostAttempt, ...])


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_cmd = commands.add_parser("inspect", help="Inspect Scout candidates and editorial statuses")
    inspect_cmd.add_argument("--output", type=Path, required=True, help="Scout data directory containing scout.sqlite3")
    inspect_cmd.add_argument("--run-id", type=UUID, default=None, help="Optional Scout run UUID filter")

    run_cmd = commands.add_parser("record-run", help="Record a Scout pilot run for yield and cost reporting")
    run_cmd.add_argument("--output", type=Path, required=True, help="Scout data directory containing scout.sqlite3")
    run_cmd.add_argument("--report", type=Path, default=None, help="Optional ScoutRunOutput JSON file")
    run_cmd.add_argument("--run-id", type=UUID, default=None, help="Run UUID when --report is not given")
    run_cmd.add_argument("--outcome", choices=["complete", "empty", "partial", "failed"], default=None)
    run_cmd.add_argument("--feed-ids", default="", help="Comma-separated contributing/polled feed IDs")
    run_cmd.add_argument("--recorded-at", default=None, help="UTC ISO timestamp for the run")
    run_cmd.add_argument("--experiment-id", default=None, help="Optional spend ledger experiment ID")
    run_cmd.add_argument("--example-id", default=None, help="Optional spend ledger example ID")
    run_cmd.add_argument("--reservation-ids", default="", help="Comma-separated reservation IDs")
    run_cmd.add_argument("--cost-records", type=Path, default=None, help="Optional JSON array of RunCostAttempt records")

    decide_cmd = commands.add_parser("decide", help="Record an append-only editorial assignment or pass decision")
    decide_cmd.add_argument("--output", type=Path, required=True, help="Scout data directory containing scout.sqlite3")
    decide_cmd.add_argument("--story-id", type=UUID, required=True)
    decide_cmd.add_argument("--revision", type=int, required=True)
    decide_cmd.add_argument("--run-id", type=UUID, required=True)
    decide_cmd.add_argument("--candidate-sha256", required=True)
    decide_cmd.add_argument("--decision", choices=["assigned", "passed"], required=True)
    decide_cmd.add_argument("--reason-code", required=True)
    decide_cmd.add_argument("--notes", default="")
    decide_cmd.add_argument("--actor-id", required=True)
    decide_cmd.add_argument("--decided-at", default=None, help="Optional UTC ISO timestamp")

    report_cmd = commands.add_parser("report", help="Generate distinct-story yield and model cost pilot report")
    report_cmd.add_argument("--output", type=Path, required=True, help="Scout data directory containing scout.sqlite3")
    report_cmd.add_argument("--ledger-db", type=Path, default=None, help="Optional separate spend ledger SQLite path")
    report_cmd.add_argument("--run-id", type=UUID, action="append", default=None, help="Filter to specific run UUID(s)")
    report_cmd.add_argument("--window-start", default=None, help="Inclusive UTC ISO start timestamp")
    report_cmd.add_argument("--window-end", default=None, help="Exclusive UTC ISO end timestamp")

    export_cmd = commands.add_parser("export-failures", help="Export rights- and privacy-checked adjudicated failures")
    export_cmd.add_argument("--output", type=Path, required=True, help="Scout data directory containing scout.sqlite3")
    export_cmd.add_argument("--out", type=Path, required=True, help="Destination JSON file path")
    export_cmd.add_argument("--dataset-version", default="next", help="Target future dataset version tag")
    export_cmd.add_argument("--rights-cleared", action="store_true", help="Explicit confirmation that source rights are cleared")
    export_cmd.add_argument("--privacy-checked", action="store_true", help="Explicit confirmation that privacy checks passed")
    export_cmd.add_argument("--run-id", type=UUID, action="append", default=None, help="Optional run UUID filter")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root: Path = args.output
    root.mkdir(parents=True, exist_ok=True)

    with closing(open_database(root / "scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(root / "artifacts", conn)

        if args.command == "inspect":
            items = list_candidates(conn, store, run_id=args.run_id)
            print(json.dumps([item.model_dump(mode="json") for item in items], indent=2))
            return 0

        if args.command == "record-run":
            run_output = (
                ScoutRunOutput.model_validate_json(args.report.read_bytes())
                if args.report is not None
                else None
            )
            cost_records = (
                _COST_RECORDS_ADAPTER.validate_json(args.cost_records.read_bytes())
                if args.cost_records is not None
                else ()
            )
            try:
                rec = record_pilot_run(
                    conn,
                    run_id=args.run_id,
                    run_output=run_output,
                    store=store,
                    recorded_at=_parse_iso(args.recorded_at),
                    outcome=args.outcome,
                    feed_ids=_parse_csv(args.feed_ids),
                    experiment_id=args.experiment_id,
                    example_id=args.example_id,
                    reservation_ids=_parse_csv(args.reservation_ids),
                    cost_records=cost_records,
                )
            except (ValueError, ValidationError) as err:
                print(json.dumps({"error": str(err)}), file=sys.stderr)
                return 2
            print(json.dumps(rec.model_dump(mode="json"), indent=2))
            return 0

        if args.command == "decide":
            try:
                dec = record_decision(
                    conn,
                    story_id=args.story_id,
                    revision=args.revision,
                    run_id=args.run_id,
                    candidate_sha256=args.candidate_sha256,
                    decision=args.decision,
                    reason_code=args.reason_code,
                    notes=args.notes,
                    actor_id=args.actor_id,
                    decided_at=_parse_iso(args.decided_at),
                )
            except (EditorialDecisionError, ValueError) as err:
                print(json.dumps({"error": str(err)}), file=sys.stderr)
                return 2
            print(json.dumps(dec.model_dump(mode="json"), indent=2))
            return 0

        if args.command == "report":
            if args.ledger_db is not None:
                with closing(open_database(args.ledger_db)) as ledger_conn:
                    report = generate_pilot_report(
                        conn,
                        ledger_conn=ledger_conn,
                        run_ids=args.run_id,
                        window_start=_parse_iso(args.window_start),
                        window_end=_parse_iso(args.window_end),
                    )
            else:
                report = generate_pilot_report(
                    conn,
                    run_ids=args.run_id,
                    window_start=_parse_iso(args.window_start),
                    window_end=_parse_iso(args.window_end),
                )
            print(json.dumps(report.model_dump(mode="json"), indent=2))
            return 0

        if args.command == "export-failures":
            try:
                bundle = export_adjudicated_failures(
                    conn,
                    store,
                    output_path=args.out,
                    rights_cleared=args.rights_cleared,
                    privacy_checked=args.privacy_checked,
                    dataset_version=args.dataset_version,
                    run_ids=args.run_id,
                )
            except ExportGateError as err:
                print(json.dumps({"error": str(err)}), file=sys.stderr)
                return 2
            print(json.dumps(bundle.model_dump(mode="json"), indent=2))
            return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
