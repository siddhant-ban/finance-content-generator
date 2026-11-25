"""Public evaluation boundary running News Scout with named OpenRouter profiles and spend cap."""
import argparse
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import Field, JsonValue, TypeAdapter

from app.contracts.common import Nonempty, Sha256, StrictModel
from app.contracts.evidence import SourceRecord
from app.contracts.scout import (
    ScoutConfiguration,
    ScoutFeedItem,
    ScoutLimits,
    ScoutRunInput,
    ScoutRunOutput,
    ScoutTokenUsage,
    ScoutTopicPolicy,
    ScoutFingerprintSnapshot,
    StoryCandidate,
)
from app.evals.scout_contracts import (
    Candidate as EvalCandidate,
    Claim as EvalClaim,
    Disposition as EvalDisposition,
    Example,
    Manifest,
    Observation as EvalObservation,
    SavedExperiment,
    SavedScoutOutput,
    Source as EvalSource,
    SourcePin,
    Warning as EvalWarning,
)
from app.observability.tracing import Telemetry
from app.prompts import load_prompt
from app.scout.eval_config import NonNegativeDecimal, load_scout_eval_config
from app.scout.ledger import LedgerSummary, SpendLedger
from app.scout.openrouter import (
    SCOUT_ENRICHMENT_PROMPT_MANIFEST,
    OpenRouterScoutEnricher,
    ScoutCallTrace,
)
from app.scout.run import Clusterer, exact_clusters, run_scout
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database

_EXAMPLES = TypeAdapter(tuple[Example, ...])
_EVAL_SOURCE_NAMESPACE = UUID("66f8d38d-1f4e-4d39-9d72-4a47b4cc2f01")
_EVAL_RELEASE_ID = UUID("91a7c45e-3b0d-4292-9234-1c4f9b0b12e8")


class ScoutEvalExampleOutcome(StrictModel):
    """Per-example execution summary produced by the public evaluation runner."""

    example_id: Nonempty
    run_id: UUID
    outcome: Literal["complete", "empty", "partial", "failed"]
    quarantined: bool
    failure_dispositions: tuple[Nonempty, ...] = ()
    reported_tokens: ScoutTokenUsage
    saved_output: SavedScoutOutput


class ScoutEvalRunResult(StrictModel):
    """Typed output envelope returned by run_scout_eval for downstream #9, #10, and #11."""

    schema_version: Literal["1.0"] = "1.0"
    experiment_id: Nonempty
    profile_name: Nonempty
    requested_model: Nonempty
    resolved_model: Nonempty | None = None
    resolved_models: tuple[Nonempty, ...] = ()
    config_hash: Sha256
    prompt_id: Nonempty
    prompt_version: Nonempty
    prompt_hash: Sha256
    outbound_call_count: int = Field(ge=0)
    preflight_call_count: int = Field(ge=0)
    completion_call_count: int = Field(ge=0)
    cost: NonNegativeDecimal
    pending_reservations: int = Field(ge=0)
    ambiguous_reservations: int = Field(ge=0)
    ledger_summary: LedgerSummary
    calls: tuple[ScoutCallTrace, ...] = ()
    examples: tuple[ScoutEvalExampleOutcome, ...] = ()
    saved_experiment: SavedExperiment


def _canonical_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _load_split_examples(dataset: Path, split: Literal["dev", "prod_regression"]) -> tuple[Manifest, str, tuple[Example, ...]]:
    manifest_raw = _canonical_json_bytes(json.loads((dataset / "manifest.json").read_bytes()))
    manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
    manifest = Manifest.model_validate_json(manifest_raw)
    split_file = "dev.json" if split == "dev" else "prod_regression.json"
    expected_sha = manifest.dev_sha256 if split == "dev" else manifest.prod_regression_sha256
    split_raw = _canonical_json_bytes(json.loads((dataset / split_file).read_bytes()))
    if hashlib.sha256(split_raw).hexdigest() != expected_sha:
        raise ValueError(f"{split}_pin_mismatch")
    examples = _EXAMPLES.validate_json(split_raw)
    return manifest, manifest_hash, examples


def _eval_source_uuid(source_id: str) -> UUID:
    return uuid5(_EVAL_SOURCE_NAMESPACE, source_id)


def _to_feed_payload(obs_source: EvalSource, disposition: EvalDisposition) -> bytes:
    source_uuid = _eval_source_uuid(obs_source.source_id)
    if disposition == "inaccessible":
        # Missing locator/excerpt triggers invalid_provenance inside run_scout.
        return json.dumps(
            {
                "headline": f"Inaccessible item {obs_source.source_id}",
                "source": {
                    "schema_version": "1.0",
                    "source_id": str(source_uuid),
                    "revision": obs_source.revision,
                    "original_url": str(obs_source.url),
                    "canonical_url": str(obs_source.url),
                    "publisher": obs_source.publisher,
                    "source_class": "primary" if obs_source.source_class == "primary" else "reputable_secondary",
                    "published_at": obs_source.published_at.isoformat() if obs_source.published_at else None,
                    "publication_time_reason": "inaccessible_source",
                    "retrieved_at": obs_source.retrieved_at.isoformat(),
                    "locator": None,
                    "excerpt": None,
                    "rights": "unknown",
                    "usage_notes": "Eval fixture inaccessible source.",
                    "content_hash": None,
                },
            }
        ).encode("utf-8")

    record = SourceRecord(
        source_id=source_uuid,
        revision=obs_source.revision,
        original_url=str(obs_source.url),
        canonical_url=str(obs_source.url),
        publisher=obs_source.publisher,
        source_class="primary" if obs_source.source_class == "primary" else "reputable_secondary",
        published_at=obs_source.published_at,
        publication_time_reason="Undated in fixture" if obs_source.published_at is None else None,
        retrieved_at=obs_source.retrieved_at,
        locator=obs_source.locator,
        excerpt=obs_source.excerpt,
        rights="excerpt_permitted" if obs_source.excerpt is not None else "metadata_only",
        usage_notes="Synthetic Scout evaluation fixture.",
    )
    item = ScoutFeedItem(headline=f"Bulletin {obs_source.source_id}", source=record)
    return item.canonical_bytes()


def _map_item_disposition(reason: str) -> EvalDisposition:
    if reason == "no_publication_time":
        return "undated"
    if reason == "outside_window":
        return "stale"
    if reason in {"invalid_provenance", "unknown_rights"}:
        return "inaccessible"
    return "candidate"


def _map_warnings(warnings: tuple[str, ...]) -> tuple[EvalWarning, ...]:
    mapping: dict[str, EvalWarning] = {
        "conflicting_sources": "conflict",
        "revision_possible": "correction",
        "limited_coverage": "uncertainty",
    }
    mapped: list[EvalWarning] = []
    for warning in warnings:
        target = mapping.get(warning)
        if target is not None and target not in mapped:
            mapped.append(target)
    return tuple(mapped)


def _project_saved_output(
    example: Example,
    run_output: ScoutRunOutput,
    store: ArtifactStore,
    uuid_to_eval_source: dict[UUID, EvalSource],
) -> SavedScoutOutput:
    item_map = {item.item_id: item for item in run_output.items}
    observations: list[EvalObservation] = []
    for obs in example.observations:
        disp = item_map.get(obs.source.source_id)
        mapped_disp: EvalDisposition = (
            _map_item_disposition(disp.reason) if disp is not None else "inaccessible"
        )
        observations.append(EvalObservation(source=obs.source, disposition=mapped_disp))

    candidates: list[EvalCandidate] = []
    for ref in run_output.candidates:
        candidate = StoryCandidate.model_validate_json(store.read(ref))
        rep_source = uuid_to_eval_source[candidate.representative.source_id]
        member_pins = tuple(
            SourcePin(
                source_id=uuid_to_eval_source[m.source.source_id].source_id,
                revision=uuid_to_eval_source[m.source.source_id].revision,
                sha256=uuid_to_eval_source[m.source.source_id].record_hash(),
            )
            for m in candidate.members
        )
        claims = tuple(
            EvalClaim(
                text=stmt.text,
                source_ids=tuple(
                    dict.fromkeys(uuid_to_eval_source[src.source_id].source_id for src in stmt.sources)
                ),
            )
            for stmt in candidate.summary
        )
        candidates.append(
            EvalCandidate(
                story_id=str(candidate.story_id),
                revision=candidate.revision,
                representative=SourcePin(
                    source_id=rep_source.source_id,
                    revision=rep_source.revision,
                    sha256=rep_source.record_hash(),
                ),
                members=member_pins,
                claims=claims,
                entities=candidate.entities,
                topics=candidate.topics,
                warnings=_map_warnings(candidate.warnings),
            )
        )

    saved_outcome: Literal["complete", "partial", "failed"] = (
        "complete" if run_output.outcome == "empty" else run_output.outcome
    )
    return SavedScoutOutput(
        observations=tuple(observations),
        candidates=tuple(candidates),
        outcome=saved_outcome,
    )


def run_scout_eval(
    *,
    dataset: Path,
    split: Literal["dev", "prod_regression"] = "dev",
    output_dir: Path,
    ledger_db_path: Path | None = None,
    experiment_id: str,
    profile_name: str | None = None,
    config_path: Path | None = None,
    allow_paid_providers: bool | None = None,
    prompt_manifest_path: Path = SCOUT_ENRICHMENT_PROMPT_MANIFEST,
    transport: httpx.BaseTransport | None = None,
    clusterer: Clusterer = exact_clusters,
    example_ids: tuple[str, ...] | None = None,
    max_tokens: int = 1500,
    timeout_seconds: float = 30.0,
) -> ScoutEvalRunResult:
    """Execute Scout evaluation examples under a named OpenRouter profile and durable spend cap."""
    manifest, manifest_hash, all_examples = _load_split_examples(dataset, split)
    if example_ids is not None:
        wanted = set(example_ids)
        selected = tuple(ex for ex in all_examples if ex.example_id in wanted)
        if len(selected) != len(wanted):
            missing = sorted(wanted - {ex.example_id for ex in selected})
            raise ValueError(f"unknown_example_ids: {missing}")
    else:
        selected = all_examples

    eval_config = load_scout_eval_config(
        config_path=config_path,
        profile_name=profile_name,
        allow_paid_providers=allow_paid_providers,
    )
    prompt = load_prompt(prompt_manifest_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_ledger_path = ledger_db_path or (output_dir / "ledger.sqlite3")
    resolved_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_database(resolved_ledger_path)) as ledger_conn:
        migrate(ledger_conn)
    ledger = SpendLedger(resolved_ledger_path)

    enricher = OpenRouterScoutEnricher(
        eval_config=eval_config,
        prompt=prompt,
        ledger=ledger,
        experiment_id=experiment_id,
        prompt_manifest_path=prompt_manifest_path,
        transport=transport,
    )

    example_outcomes: list[ScoutEvalExampleOutcome] = []
    saved_outputs: dict[str, JsonValue] = {}

    with closing(open_database(output_dir / "scout.sqlite3")) as scout_conn:
        migrate(scout_conn)
        store = ArtifactStore(output_dir / "artifacts", scout_conn)
        telemetry = Telemetry(output_dir / "telemetry.sqlite3")
        try:
            for example in selected:
                batch: dict[str, bytes] = {
                    obs.source.source_id: _to_feed_payload(obs.source, obs.disposition)
                    for obs in example.observations
                }
                uuid_to_eval_source = {
                    _eval_source_uuid(obs.source.source_id): obs.source
                    for obs in example.observations
                }
                feed_snapshot = hashlib.sha256(
                    _canonical_json_bytes({"items": {k: v.decode("utf-8") for k, v in batch.items()}})
                ).hexdigest()
                request = ScoutRunInput(
                    schema_version="1.0",
                    run_id=uuid4(),
                    invocation_id=uuid4(),
                    window_start=example.window_start,
                    window_end=example.window_end,
                    topic_policy=ScoutTopicPolicy(version="v1", topics=("prices", "jobs", "borrowing")),
                    fingerprints=ScoutFingerprintSnapshot(version="v1", known=()),
                    configuration=ScoutConfiguration(
                        version=f"v{eval_config.schema_version}",
                        feed_snapshot=feed_snapshot,
                        cluster_version="v1",
                        prompt_version=prompt.prompt_version,
                        prompt_hash=prompt.sha256,
                        provider="openrouter",
                        model=eval_config.model_id,
                        temperature=0.0,
                    ),
                    release_id=_EVAL_RELEASE_ID,
                    limits=ScoutLimits(
                        max_items=max(1, len(example.observations)),
                        max_tokens=max_tokens,
                        timeout_seconds=timeout_seconds,
                    ),
                )

                trace_start = len(enricher.traces)

                def _enrich_for_example(
                    req: ScoutRunInput,
                    members: tuple,
                    *,
                    max_tokens: int,
                    timeout_seconds: float,
                    _ex_id: str = example.example_id,
                ):
                    result = enricher.enrich_cluster(
                        req,
                        members,
                        max_tokens=max_tokens,
                        timeout_seconds=timeout_seconds,
                        example_id=_ex_id,
                    )
                    assert result.model_result is not None
                    return result.model_result

                run_output = run_scout(
                    request,
                    store=store,
                    telemetry=telemetry,
                    fetch=lambda *_args, _b=batch, **_kwargs: _b,
                    enrich=_enrich_for_example,
                    cluster=clusterer,
                )
                ex_traces = enricher.traces[trace_start:]
                dispositions = tuple(
                    dict.fromkeys(
                        t.failure_disposition for t in ex_traces if t.failure_disposition is not None
                    )
                )
                saved_output = _project_saved_output(example, run_output, store, uuid_to_eval_source)
                saved_outputs[example.example_id] = saved_output.model_dump(mode="json")
                example_outcomes.append(
                    ScoutEvalExampleOutcome(
                        example_id=example.example_id,
                        run_id=run_output.run_id,
                        outcome=run_output.outcome,
                        quarantined=any(t.quarantined for t in ex_traces) or run_output.outcome in {"partial", "failed"},
                        failure_dispositions=dispositions,
                        reported_tokens=run_output.reported_tokens,
                        saved_output=saved_output,
                    )
                )
        finally:
            telemetry.shutdown(timeout_seconds=0)

    ledger_summary = ledger.summary(experiment_id)
    run_cost = sum(
        (t.cost for t in enricher.traces if t.cost is not None),
        Decimal("0"),
    )
    resolved_models = tuple(
        dict.fromkeys(t.resolved_model for t in enricher.traces if t.resolved_model is not None)
    )
    saved_experiment = SavedExperiment(
        schema_version="1.0",
        dataset_version=manifest.dataset_version,
        dataset_manifest_sha256=manifest_hash,
        evaluator_version=manifest.evaluator_version,
        split=split,
        prompt_version=prompt.prompt_version,
        model_id=eval_config.model_id,
        outputs=saved_outputs,
    )
    result = ScoutEvalRunResult(
        experiment_id=experiment_id,
        profile_name=eval_config.profile_name,
        requested_model=eval_config.model_id,
        resolved_model=resolved_models[0] if len(resolved_models) == 1 else None,
        resolved_models=resolved_models,
        config_hash=eval_config.config_hash(),
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.prompt_version,
        prompt_hash=prompt.sha256,
        outbound_call_count=enricher.outbound_call_count,
        preflight_call_count=enricher.preflight_call_count,
        completion_call_count=enricher.completion_call_count,
        cost=run_cost,
        pending_reservations=ledger_summary.pending_reservations,
        ambiguous_reservations=ledger_summary.ambiguous_reservations,
        ledger_summary=ledger_summary,
        calls=tuple(enricher.traces),
        examples=tuple(example_outcomes),
        saved_experiment=saved_experiment,
    )
    (output_dir / "saved_experiment.json").write_bytes(saved_experiment.canonical_bytes())
    (output_dir / "eval_report.json").write_bytes(result.canonical_bytes())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--split", choices=["dev", "prod_regression"], default="dev")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger-db", type=Path)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--allow-paid-providers", action="store_true", default=None)
    args = parser.parse_args(argv)

    result = run_scout_eval(
        dataset=args.dataset,
        split=args.split,
        output_dir=args.output,
        ledger_db_path=args.ledger_db,
        experiment_id=args.experiment_id,
        profile_name=args.profile,
        config_path=args.config,
        allow_paid_providers=args.allow_paid_providers,
    )
    print(
        json.dumps(
            {
                "experiment_id": result.experiment_id,
                "profile_name": result.profile_name,
                "requested_model": result.requested_model,
                "resolved_models": list(result.resolved_models),
                "config_hash": result.config_hash,
                "outbound_call_count": result.outbound_call_count,
                "cost": str(result.cost),
                "pending_reservations": result.pending_reservations,
                "ambiguous_reservations": result.ambiguous_reservations,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
