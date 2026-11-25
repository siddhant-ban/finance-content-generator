"""Run Scout locally against a permitted recording and inspect its results."""
import argparse
from contextlib import closing
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import JsonValue, ValidationError

from app.contracts.common import Nonempty, StrictModel
from app.contracts.scout import (
    ScoutMember, ScoutRunInput, ScoutRunOutput,
    ScoutTokenUsage, StoryCandidate, candidate_fingerprint,
)
from app.observability.tracing import Telemetry
from app.scout.run import FeedPayload, ModelResult, ScoutRequestConflict, TokenBudgetExceeded, run_scout
from app.scout.workflow import all_story_revisions, story_revisions
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database


class RecordedFeed(StrictModel):
    items: dict[Nonempty, JsonValue]


class RecordedResponse(StrictModel):
    output: JsonValue
    usage: ScoutTokenUsage


class ScoutRecording(StrictModel):
    schema_version: Literal["1.0"]
    feed: RecordedFeed
    responses: dict[str, JsonValue]

    def fetch(self, request: ScoutRunInput, *, timeout_seconds: float) -> dict[str, FeedPayload]:
        return dict(sorted(self.feed.items.items()))

    def enrich(self, request: ScoutRunInput, members: tuple[ScoutMember, ...], *,
               max_tokens: int, timeout_seconds: float) -> ModelResult:
        url = members[0].source.canonical_url
        raw = self.responses.get(f"{url}#{candidate_fingerprint(members)}")
        if raw is None:
            raw = self.responses.get(url)
        if raw is None:
            raise OSError("recording is missing the cluster response")
        response = RecordedResponse.model_validate(raw)
        if response.usage.total > max_tokens:
            raise TokenBudgetExceeded
        return ModelResult(json.dumps(response.output).encode("utf-8"), response.usage)


def _display(value: str) -> str:
    # Feed and model strings are data, including when rendered in a terminal.
    return "".join(char if char.isprintable() else " " for char in value)


def _format_candidate(
    candidate: StoryCandidate,
    *,
    reason_code: str | None = None,
    artifact_sha256: str | None = None,
) -> list[str]:
    lines = [
        "",
        _display(candidate.representative_member.headline),
        f"Story {candidate.story_id}  Cluster {candidate.cluster_id}  Revision {candidate.revision}",
        f"Fingerprint {candidate.fingerprint}",
    ]
    if artifact_sha256 is not None:
        lines.append(f"Artifact {artifact_sha256}")
    if reason_code is not None:
        lines.append(f"Reason {_display(reason_code)}")
    lines.extend([
        "Topics  " + ", ".join(candidate.topics),
        "Entities  " + ", ".join(map(_display, candidate.entities)),
        "Warnings  " + ", ".join(candidate.warnings),
    ])
    for label, statements in (("Summary", candidate.summary), ("Uncertainty", candidate.uncertainty)):
        for statement in statements:
            pins = ", ".join(f"{pin.source_id}/{pin.source_revision}" for pin in statement.sources)
            lines.append(f"{label}  {_display(statement.text)} [{pins}]")
    for member in candidate.members:
        source = member.source
        assert source.published_at is not None  # Checked by ScoutMember.
        lines.extend([
            f"Source  {_display(source.publisher)} ({source.source_class})",
            f"URL  {_display(source.canonical_url)}",
            f"Original URL  {_display(source.original_url)}",
            f"Published {source.published_at.isoformat()}  Retrieved {source.retrieved_at.isoformat()}",
            f"Pin {source.source_id}/{source.revision}  {member.ref.source_record_hash}",
        ])
        if source.locator:
            lines.append(f"Locator  {_display(source.locator)}")
        if source.excerpt:
            lines.append(f"Permitted excerpt  {_display(source.excerpt)}")
    return lines


def format_result(report: ScoutRunOutput, store: ArtifactStore) -> str:
    lines = [f"Scout run {report.run_id}  {report.outcome}", f"Trace {report.trace_id}"]
    if report.issue:
        lines.append(f"Run issue  {report.issue}")
    lines += [f"{_display(item.item_id)}  {item.status} ({item.reason})" for item in report.items]
    for ref in report.candidates:
        candidate = StoryCandidate.model_validate_json(store.read(ref))
        lines.extend(_format_candidate(candidate))
        revisions = story_revisions(store, candidate.story_id)
        prior = [record for record in revisions if record.revision < candidate.revision]
        if prior:
            lines.append(f"Prior revisions ({len(prior)})")
            for record in prior:
                lines.extend(_format_candidate(
                    record.candidate,
                    reason_code=record.reason_code,
                    artifact_sha256=record.artifact_ref.sha256,
                ))
    return "\n".join(lines)


def format_history(store: ArtifactStore, story_id: UUID | None = None) -> str:
    revisions = story_revisions(store, story_id) if story_id is not None else all_story_revisions(store)
    if not revisions:
        return "No recorded Scout story revisions."
    lines: list[str] = []
    for record in revisions:
        lines.extend(_format_candidate(
            record.candidate,
            reason_code=record.reason_code,
            artifact_sha256=record.artifact_ref.sha256,
        ))
    return "\n".join(lines).lstrip("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", type=Path, required=True, help="ScoutRunInput JSON with an explicit UTC window")
    run.add_argument("--recording", type=Path, required=True, help="Versioned feed and model recording JSON")
    run.add_argument("--output", type=Path, required=True, help="Local SQLite, artifact and report directory")
    history = commands.add_parser("history")
    history.add_argument("--output", type=Path, required=True, help="Local SQLite and artifact directory")
    history.add_argument("--story-id", type=UUID, required=False, help="Optional story UUID filter")
    args = parser.parse_args(argv)

    if args.command == "history":
        root = args.output
        with closing(open_database(root / "scout.sqlite3")) as conn:
            migrate(conn)
            store = ArtifactStore(root / "artifacts", conn)
            print(format_history(store, args.story_id))
        return 0

    try:
        request = ScoutRunInput.model_validate_json(args.request.read_bytes())
        recording = ScoutRecording.model_validate_json(args.recording.read_bytes())
    except (OSError, ValidationError):
        parser.error("could not read a valid Scout request and recording")
    if request.configuration.feed_snapshot != recording.feed.record_hash():
        parser.error("recorded feed does not match the frozen feed snapshot")

    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    with closing(open_database(root / "scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(root / "artifacts", conn)
        telemetry = Telemetry(root / "scout-telemetry.sqlite3")
        try:
            report = run_scout(request, store=store, telemetry=telemetry,
                               fetch=recording.fetch, enrich=recording.enrich)
        except ScoutRequestConflict as error:
            parser.error(str(error))
        finally:
            telemetry.shutdown(timeout_seconds=0)
        (root / "report.json").write_bytes(report.canonical_bytes())
        print(format_result(report, store))
    return int(report.outcome == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
