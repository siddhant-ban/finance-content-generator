"""The editor and Novelty consume the same recorded Scout artifact."""
import json
from hashlib import sha256
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.contracts.scout import ScoutRunInput, ScoutRunOutput, StoryCandidate
from app.contracts.workflow import StoryState
from app.scout.cli import main
from app.scout.workflow import scout_state
from app.storage.artifacts import ArtifactStore
from app.storage.db import open_database

RECORDING = Path(__file__).parents[1] / "fixtures" / "scout" / "bls-cpi.json"


def run_request(**overrides):
    fields = json.loads(RECORDING.with_name("bls-cpi-request.json").read_bytes())
    fields.update(run_id=str(uuid4()), invocation_id=str(uuid4()))
    return ScoutRunInput.model_validate_json(json.dumps(fields | overrides))


def test_editor_runs_one_recorded_release_and_novelty_reads_persisted_candidate(tmp_path, capsys):
    request = run_request()
    input_path = tmp_path / "request.json"
    input_path.write_bytes(request.canonical_bytes())
    root = tmp_path / "scout"

    result = main(["run", "--request", str(input_path), "--recording", str(RECORDING), "--output", str(root)])

    assert result == 0
    visible = capsys.readouterr().out
    assert "Consumer Price Index - August 2025" in visible
    assert "U.S. Bureau of Labor Statistics" in visible
    assert "https://www.bls.gov/news.release/archives/cpi_09112025.htm" in visible
    assert "2025-09-11T12:30:00" in visible
    assert "2026-11-25T01:30:22" in visible
    assert "Opening paragraph" in visible
    assert "seasonally adjusted" in visible
    assert "primary" in visible
    assert "prices" in visible

    batch = ScoutRunOutput.model_validate_json((root / "report.json").read_bytes())
    assert batch.run_id == request.run_id
    assert batch.outcome == "complete"
    assert len(batch.trace_id) == 32
    assert [item.status for item in batch.items] == ["new"]
    (candidate_ref,) = batch.candidates

    with closing(open_database(root / "scout.sqlite3")) as conn:
        store = ArtifactStore(root / "artifacts", conn)
        candidate = StoryCandidate.model_validate_json(store.read(candidate_ref))
        snapshot = scout_state(store).get(candidate.story_id)
        assert snapshot.artifacts["candidate"] == snapshot.artifacts["cluster"] == candidate_ref
        assert snapshot.state == StoryState.CLUSTERED
        assert [event.to_state for event in scout_state(store).events(candidate.story_id)] == [
            StoryState.DISCOVERED, StoryState.CLUSTERED,
        ]
        assert ScoutRunInput.model_validate_json(store.read(batch.input_ref)) == request

    (member,) = candidate.members
    assert candidate.revision == member.source.revision == 1
    assert candidate.cluster_id != candidate.story_id
    assert candidate.representative == candidate.summary[0].sources[0] == member.ref
    assert member.source.published_at == datetime(2025, 9, 11, 12, 30, tzinfo=timezone.utc)
    assert member.ref.source_record_hash in visible
    assert candidate.fingerprint in visible
    assert "Consumer Price Index" in candidate.entities
    assert "seasonally_adjusted" in candidate.warnings


def test_recorded_cli_accounts_for_invalid_response_and_item_beyond_limit(tmp_path):
    request = run_request(limits={"max_items": 1, "max_tokens": 200, "timeout_seconds": 10})
    recording = json.loads(RECORDING.read_bytes())
    recording["feed"]["items"]["z-over-item-limit"] = {"unsupported": True}
    response = next(iter(recording["responses"].values()))
    response["output"]["schema_version"] = "2.0"
    feed_bytes = json.dumps(
        recording["feed"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    config = request.configuration.model_copy(update={"feed_snapshot": sha256(feed_bytes).hexdigest()})
    request = request.model_copy(update={"configuration": config})
    input_path = tmp_path / "request.json"
    input_path.write_bytes(request.canonical_bytes())
    recording_path = tmp_path / "recording.json"
    recording_path.write_text(json.dumps(recording))
    root = tmp_path / "scout"

    assert main([
        "run", "--request", str(input_path), "--recording", str(recording_path), "--output", str(root),
    ]) == 0

    batch = ScoutRunOutput.model_validate_json((root / "report.json").read_bytes())
    assert batch.outcome == "partial"
    assert batch.candidates == ()
    assert [(item.status, item.reason) for item in batch.items] == [
        ("failed", "invalid_enrichment"), ("unprocessed", "item_limit"),
    ]
