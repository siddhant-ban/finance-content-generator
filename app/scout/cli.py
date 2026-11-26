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
from app.scout.feed_cli import add_feed_commands, manage_feeds
from app.scout.replay import (
    ScoutAuditReplayOutput,
    ScoutReevalOutput,
    reeval_scout,
    replay_scout,
)
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
    if report.feeds is not None:
        for feed in report.feeds:
            if feed.status in {"read", "not_modified"}:
                detail = f"{len(feed.item_ids)} items" if feed.item_ids else "quiet"
            else:
                detail = feed.reason or feed.status
            lines.append(f"Feed {_display(feed.feed_id)} r{feed.revision}  {feed.status} ({detail})")
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


def format_audit_replay(report: ScoutAuditReplayOutput) -> str:
    cov = report.replay_coverage
    lines = [
        f"Mode: {report.replay_mode}  Network: {'disabled' if report.network_disabled else 'enabled'}  Dispatch: disabled",
        f"Source run {report.source_run_id}  Invocation {report.source_invocation_id}",
        f"Source input {report.source_input_sha256}",
        f"Config {report.config_hash}  Prompt {report.prompt_hash}  Feed {report.feed_snapshot}",
        f"Provider {_display(report.provider)}  Model {_display(report.model)}  Cluster {_display(report.cluster_version)}",
        f"Coverage: {cov.status} ({cov.replayable_inputs}/{cov.total_inputs} replayable, {cov.unreplayable_inputs} unreplayable)",
        f"All hashes matched: {report.all_hashes_matched}  Tokens: {report.reported_tokens.input_tokens}+{report.reported_tokens.output_tokens}",
    ]
    for unreplayable in report.unreplayable_items:
        lines.append(
            f"Unreplayable {_display(unreplayable.item_id)}  rights={unreplayable.rights}  reason={_display(unreplayable.reason)}"
        )
    for item in report.candidates:
        lines.append("")
        lines.append(f"Replayed Story {item.story_id}  Cluster {item.cluster_id}  Revision {item.revision}")
        lines.append(f"Original SHA256 {item.original_artifact_sha256}  Reproduced SHA256 {item.reproduced_artifact_sha256}  Match {item.hash_matches}")
        lines.extend(_format_candidate(item.candidate, artifact_sha256=item.reproduced_artifact_sha256))
    return "\n".join(lines)


def format_reeval(report: ScoutReevalOutput) -> str:
    cov = report.replay_coverage
    lines = [
        f"Mode: {report.replay_mode}  Network: {'disabled' if report.network_disabled else 'enabled'}  Dispatch: disabled",
        f"Run {report.run_id}  Invocation {report.invocation_id}  Trace {report.trace_id}",
        f"Source run {report.source_run_id}  Source invocation {report.source_invocation_id}",
        f"Source input {report.source_input_sha256}  Input {report.input_sha256}",
        f"Experiment {_display(report.experiment_id)}  Spend cap USD {report.hard_spend_cap_usd}",
        f"Source model {_display(report.source_model)}  Source prompt {report.source_prompt_hash}  Source config {report.source_config_hash}",
        f"Provider {_display(report.provider)}  Model {_display(report.model)}  Prompt {_display(report.prompt_version)} ({report.prompt_hash})  Config {report.config_hash}",
        f"Coverage: {cov.status} ({cov.replayable_inputs}/{cov.total_inputs} replayable, {cov.unreplayable_inputs} unreplayable)",
        f"Tokens: {report.reported_tokens.input_tokens}+{report.reported_tokens.output_tokens}",
    ]
    for unreplayable in report.unreplayable_items:
        lines.append(
            f"Unreplayable {_display(unreplayable.item_id)}  rights={unreplayable.rights}  reason={_display(unreplayable.reason)}"
        )
    for item in report.candidates:
        lines.append("")
        lines.append(f"Re-evaluated Story {item.story_id}  Cluster {item.cluster_id}  Revision {item.revision}")
        lines.append(f"Source SHA256 {item.original_artifact_sha256}  Reeval SHA256 {item.reproduced_artifact_sha256}")
        lines.extend(_format_candidate(item.candidate, artifact_sha256=item.reproduced_artifact_sha256))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add_feed_commands(commands.add_parser("feeds"))
    run = commands.add_parser("run")
    run.add_argument("--request", type=Path, required=True, help="ScoutRunInput JSON with an explicit UTC window")
    run.add_argument("--recording", type=Path, required=True, help="Versioned feed and model recording JSON")
    run.add_argument("--output", type=Path, required=True, help="Local SQLite, artifact and report directory")
    history = commands.add_parser("history")
    history.add_argument("--output", type=Path, required=True, help="Local SQLite and artifact directory")
    history.add_argument("--story-id", type=UUID, required=False, help="Optional story UUID filter")

    replay = commands.add_parser("replay")
    replay.add_argument("--output", type=Path, required=True, help="Local SQLite and artifact directory")
    replay.add_argument("--run-id", type=UUID, required=False, help="Source Scout run UUID")
    replay.add_argument("--story-id", type=UUID, required=False, help="Target story UUID")
    replay.add_argument("--revision", type=int, required=False, help="Optional story revision number")
    replay.add_argument("--json", action="store_true", help="Emit canonical JSON report")

    reeval = commands.add_parser("reeval")
    reeval.add_argument("--output", type=Path, required=True, help="Local SQLite and artifact directory")
    reeval.add_argument("--run-id", type=UUID, required=False, help="Source Scout run UUID")
    reeval.add_argument("--story-id", type=UUID, required=False, help="Target story UUID")
    reeval.add_argument("--revision", type=int, required=False, help="Optional story revision number")
    reeval.add_argument("--recording", type=Path, required=False, help="Offline model response recording JSON")
    reeval.add_argument("--model", type=str, required=False, help="Override model identifier")
    reeval.add_argument("--provider", type=str, required=False, help="Override provider identifier")
    reeval.add_argument("--prompt-version", type=str, required=False, help="Override prompt version")
    reeval.add_argument("--prompt-hash", type=str, required=False, help="Override prompt SHA-256 hash")
    reeval.add_argument("--temperature", type=float, required=False, help="Override temperature")
    reeval.add_argument("--max-tokens", type=int, required=False, help="Override max token budget")
    reeval.add_argument("--experiment-id", type=str, required=False, help="Distinct experiment budget identifier")
    reeval.add_argument("--json", action="store_true", help="Emit canonical JSON report")

    args = parser.parse_args(argv)
    if args.command == "feeds":
        try:
            return manage_feeds(args)
        except ValueError as error:
            parser.error(str(error))

    if args.command == "history":
        root = args.output
        with closing(open_database(root / "scout.sqlite3")) as conn:
            migrate(conn)
            store = ArtifactStore(root / "artifacts", conn)
            print(format_history(store, args.story_id))
        return 0

    if args.command == "replay":
        if args.run_id is None and args.story_id is None:
            parser.error("replay requires --run-id or --story-id")
        root = args.output
        with closing(open_database(root / "scout.sqlite3")) as conn:
            migrate(conn)
            store = ArtifactStore(root / "artifacts", conn)
            try:
                report = replay_scout(store, run_id=args.run_id, story_id=args.story_id, revision=args.revision)
            except (OSError, ValueError, ValidationError) as error:
                parser.error(str(error))
            print(report.canonical_bytes().decode("utf-8") if args.json else format_audit_replay(report))
        return int(any(item.hash_matches is False for item in report.candidates))

    if args.command == "reeval":
        if args.run_id is None and args.story_id is None:
            parser.error("reeval requires --run-id or --story-id")
        if args.recording is None:
            parser.error("reeval requires --recording for offline evaluation")
        try:
            recording = ScoutRecording.model_validate_json(args.recording.read_bytes())
        except (OSError, ValidationError):
            parser.error("could not read a valid Scout recording")
        root = args.output
        with closing(open_database(root / "scout.sqlite3")) as conn:
            migrate(conn)
            store = ArtifactStore(root / "artifacts", conn)
            telemetry = Telemetry(root / "scout-telemetry.sqlite3")
            try:
                report = reeval_scout(
                    store,
                    enrich=recording.enrich,
                    source_run_id=args.run_id,
                    story_id=args.story_id,
                    revision=args.revision,
                    model=args.model,
                    provider=args.provider,
                    prompt_version=args.prompt_version,
                    prompt_hash=args.prompt_hash,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    experiment_id=args.experiment_id,
                    telemetry=telemetry,
                )
            except (OSError, ValueError, ValidationError) as error:
                parser.error(str(error))
            finally:
                telemetry.shutdown(timeout_seconds=0)
            print(report.canonical_bytes().decode("utf-8") if args.json else format_reeval(report))
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
