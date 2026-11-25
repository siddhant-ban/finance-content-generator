"""Integration tests for OpenRouter Scout enrichment, pre-flight checks, and eval runner."""
from contextlib import closing
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.contracts.scout import ScoutEnrichment, ScoutFeedItem, ScoutRunInput, StoryCandidate
from app.evals.scout import evaluate_experiment
from app.evals.scout_runner import run_scout_eval
from app.observability.models import SpanRecord
from app.observability.tracing import Telemetry
from app.prompts import load_prompt
from app.scout.eval_config import load_scout_eval_config
from app.scout.ledger import SpendCapExceeded, SpendLedger
from app.scout.openrouter import (
    SCOUT_ENRICHMENT_PROMPT_MANIFEST,
    OpenRouterScoutEnricher,
    PreFlightError,
)
from app.scout.run import run_scout
from app.storage import artifacts as artifacts_module
from app.storage.artifacts import ArtifactStore
from app.storage.db import migrate, open_database


TEST_KEY = "sk-or-v1-synthetic-scout-test-key"
RECORDING = Path(__file__).parents[1] / "fixtures" / "scout" / "bls-cpi.json"
REQUEST_FIXTURE = Path(__file__).parents[1] / "fixtures" / "scout" / "bls-cpi-request.json"
DATASET = Path("evals/news_scout/1.0.0")


if os.name == "nt":
    @pytest.fixture(autouse=True)
    def _windows_artifact_dir_sync_compat(monkeypatch):
        """Windows cannot open directories with os.O_RDONLY for POSIX directory fsync."""
        real_open = os.open
        real_fsync = os.fsync
        real_close = os.close
        skip_fds: set[int] = set()
        next_fd = 900_000

        def compat_open(path, flags, *args, **kwargs):
            if os.path.isdir(path) and flags == os.O_RDONLY:
                nonlocal next_fd
                next_fd += 1
                skip_fds.add(next_fd)
                return next_fd
            return real_open(path, flags, *args, **kwargs)

        def compat_fsync(fd):
            if fd in skip_fds:
                return None
            return real_fsync(fd)

        def compat_close(fd):
            if fd in skip_fds:
                skip_fds.remove(fd)
                return None
            return real_close(fd)

        class _CompatOS:
            O_RDONLY = os.O_RDONLY
            walk = staticmethod(os.walk)
            fdopen = staticmethod(os.fdopen)
            replace = staticmethod(os.replace)
            open = staticmethod(compat_open)
            fsync = staticmethod(compat_fsync)
            close = staticmethod(compat_close)

        monkeypatch.setattr(artifacts_module, "os", _CompatOS())


def _endpoint_payload(
    *,
    model_id: str = "inclusionai/ling-3.2-flash:free",
    prompt_price: str = "0",
    completion_price: str = "0",
    extra_pricing: dict[str, object] | None = None,
    supported_parameters: list[str] | None = None,
    status: object = 0,
    endpoints: list[dict[str, object]] | None = None,
    data_policy: dict[str, object] | None = None,
    data_collection: str | None = None,
) -> dict[str, object]:
    if endpoints is not None:
        return {"data": {"id": model_id, "endpoints": endpoints}}
    pricing: dict[str, object] = {
        "prompt": prompt_price,
        "completion": completion_price,
        "request": "0",
        "image": "0",
        "web_search": "0",
    }
    if extra_pricing:
        pricing.update(extra_pricing)
    endpoint: dict[str, object] = {
        "name": "recorded-endpoint",
        "model_name": model_id,
        "status": status,
        "supported_parameters": (
            ["structured_outputs", "response_format", "max_tokens", "temperature"]
            if supported_parameters is None
            else supported_parameters
        ),
        "pricing": pricing,
    }
    if data_policy is not None:
        endpoint["data_policy"] = data_policy
    if data_collection is not None:
        endpoint["data_collection"] = data_collection
    return {"data": {"id": model_id, "endpoints": [endpoint]}}


def _completion_payload(
    content: str,
    *,
    model: str = "inclusionai/ling-3.2-flash:free",
    prompt_tokens: int = 80,
    completion_tokens: int = 41,
    cost: float | None = 0.0,
) -> dict[str, object]:
    usage: dict[str, object] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
    if cost is not None:
        usage["cost"] = cost
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage,
    }


def _recording_transport(handler):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    return httpx.MockTransport(handle), requests


def _scout_request(eval_config, prompt):
    fields = json.loads(REQUEST_FIXTURE.read_bytes())
    fields["run_id"] = str(uuid4())
    fields["invocation_id"] = str(uuid4())
    fields["limits"]["max_tokens"] = 1000
    fields["configuration"].update(
        provider="openrouter",
        model=eval_config.model_id,
        prompt_version=prompt.prompt_version,
        prompt_hash=prompt.sha256,
    )
    return ScoutRunInput.model_validate_json(json.dumps(fields))


def test_pinned_scout_prompt_exists_and_tamper_stops_before_http(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    prompt = load_prompt(SCOUT_ENRICHMENT_PROMPT_MANIFEST)
    assert prompt.prompt_id == "scout_enrichment"
    assert prompt.prompt_version == "scout_enrichment.v1"

    # Copy prompt & manifest to tmp_path and tamper with prompt bytes after pinning.
    prompt_dir = tmp_path / "prompts"
    (prompt_dir / "manifests").mkdir(parents=True)
    (prompt_dir / "scout").mkdir(parents=True)
    manifest = json.loads(SCOUT_ENRICHMENT_PROMPT_MANIFEST.read_bytes())
    prompt_file = prompt_dir / "scout" / "enrichment.v1.md"
    original = (SCOUT_ENRICHMENT_PROMPT_MANIFEST.parent / manifest["path"]).read_bytes()
    prompt_file.write_bytes(original)
    manifest_file = prompt_dir / "manifests" / "scout_enrichment.v1.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    pinned = load_prompt(manifest_file)
    prompt_file.write_bytes(original + b"\nInjected instruction")

    db_path = tmp_path / "ledger.sqlite3"
    with closing(open_database(db_path)) as conn:
        migrate(conn)
    transport, requests = _recording_transport(lambda _req: httpx.Response(200, json={}))
    eval_config = load_scout_eval_config()
    enricher = OpenRouterScoutEnricher(
        eval_config=eval_config,
        prompt=pinned,
        prompt_manifest_path=manifest_file,
        ledger=SpendLedger(db_path),
        experiment_id="exp-tamper",
        transport=transport,
    )
    recording = json.loads(RECORDING.read_bytes())
    item = ScoutFeedItem.model_validate_json(json.dumps(next(iter(recording["feed"]["items"].values()))))
    req = _scout_request(eval_config, pinned)
    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "artifacts", conn)
        telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
        try:
            output = run_scout(
                req,
                store=store,
                telemetry=telemetry,
                fetch=lambda *_a, **_k: {"bls": item.canonical_bytes()},
                enrich=enricher,
            )
        finally:
            telemetry.shutdown(timeout_seconds=0)

    assert requests == []
    assert output.outcome == "failed"
    assert output.items[0].reason == "model_unavailable"


@pytest.mark.parametrize(
    "case,ep_kwargs",
    [
        ("empty_endpoints", {"endpoints": []}),
        ("unavailable_status", {"status": -2}),
        ("no_structured_outputs", {"supported_parameters": ["max_tokens", "temperature"]}),
        ("data_collection_allow", {"data_collection": "allow"}),
        ("trains_on_prompts", {"data_policy": {"trains_on_prompts": True, "retains_prompts": False}}),
        ("retains_prompts", {"data_policy": {"trains_on_prompts": False, "retains_prompts": True}}),
        ("negative_unbounded_price", {"prompt_price": "-1"}),
        ("higher_rate", {"prompt_price": "0.0000002", "completion_price": "0.0000006"}),
        ("nonzero_request_fee", {"extra_pricing": {"request": "0.001"}}),
        ("nonzero_image_fee", {"extra_pricing": {"image": "0.01"}}),
        ("nonzero_web_search_fee", {"extra_pricing": {"web_search": "0.005"}}),
    ],
)
def test_preflight_blocks_unsafe_endpoints_before_completion(tmp_path, monkeypatch, case, ep_kwargs):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    toml_path = tmp_path / "paid.toml"
    toml_path.write_text(
        """
schema_version = 1
default_profile = "paid"
hard_spend_cap_usd = "0.05"

[profiles.paid]
model_id = "example/model-1"
input_usd_per_million_tokens = "0.15"
output_usd_per_million_tokens = "0.60"
""",
        encoding="utf-8",
    )
    eval_config = load_scout_eval_config(config_path=toml_path, allow_paid_providers=True)
    prompt = load_prompt(SCOUT_ENRICHMENT_PROMPT_MANIFEST)
    db_path = tmp_path / "ledger.sqlite3"
    with closing(open_database(db_path)) as conn:
        migrate(conn)
    ledger = SpendLedger(db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/models/example/model-1/endpoints"
        return httpx.Response(200, json=_endpoint_payload(model_id="example/model-1", **ep_kwargs))

    transport, requests = _recording_transport(handler)
    enricher = OpenRouterScoutEnricher(
        eval_config=eval_config,
        prompt=prompt,
        ledger=ledger,
        experiment_id=f"exp-{case}",
        transport=transport,
    )
    recording = json.loads(RECORDING.read_bytes())
    item = ScoutFeedItem.model_validate_json(json.dumps(next(iter(recording["feed"]["items"].values()))))
    req = _scout_request(eval_config, prompt)

    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "artifacts", conn)
        telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
        try:
            output = run_scout(
                req,
                store=store,
                telemetry=telemetry,
                fetch=lambda *_a, **_k: {"bls": item.canonical_bytes()},
                enrich=enricher,
            )
        finally:
            telemetry.shutdown(timeout_seconds=0)

    # Pre-flight GET was made, but NO POST /chat/completions was sent.
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert "Authorization" not in requests[0].headers
    assert output.outcome == "failed"
    assert output.items[0].reason == "model_unavailable"
    summary = ledger.summary(f"exp-{case}")
    assert summary.pending_reservations == 0
    assert summary.ambiguous_reservations == 0
    assert summary.reconciled_cost_usd == Decimal("0")


def test_run_scout_with_openrouter_adapter_enforces_routing_and_continues_on_invalid_cluster(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    eval_config = load_scout_eval_config()
    prompt = load_prompt(SCOUT_ENRICHMENT_PROMPT_MANIFEST)
    db_path = tmp_path / "ledger.sqlite3"
    with closing(open_database(db_path)) as conn:
        migrate(conn)
    ledger = SpendLedger(db_path)

    recording = json.loads(RECORDING.read_bytes())
    first_item = ScoutFeedItem.model_validate_json(json.dumps(next(iter(recording["feed"]["items"].values()))))
    second_payload = first_item.model_dump(mode="json")
    second_payload["headline"] = "Second synthetic release"
    second_payload["source"].update(
        source_id=str(uuid4()),
        canonical_url="https://example.invalid/second-release",
        original_url="https://example.invalid/second-release",
    )
    second_item = ScoutFeedItem.model_validate_json(json.dumps(second_payload))
    second_ref = {
        "source_id": str(second_item.source.source_id),
        "source_revision": second_item.source.revision,
        "source_record_hash": second_item.source.record_hash(),
    }

    call_idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        if request.method == "GET":
            return httpx.Response(200, json=_endpoint_payload(model_id=eval_config.model_id))
        call_idx += 1
        assert request.url == httpx.URL("https://openrouter.ai/api/v1/chat/completions")
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        body = json.loads(request.content)
        assert body["model"] == eval_config.model_id
        assert body["provider"] == {
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "max_price": {"prompt": 0.0, "completion": 0.0},
        }
        assert "plugins" not in body
        assert "web_search_options" not in body
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        if call_idx == 1:
            # First cluster returns foreign ref -> quarantined, second cluster still runs!
            bad = {
                "schema_version": "1.0",
                "summary": [{"text": "Forged citation", "sources": [{
                    "source_id": str(uuid4()), "source_revision": 1, "source_record_hash": "0" * 64,
                }]}],
                "topics": ["prices"],
                "entities": ["BLS"],
                "warnings": [],
            }
            return httpx.Response(200, json=_completion_payload(json.dumps(bad)))
        good = {
            "schema_version": "1.0",
            "summary": [{"text": "Valid second cluster summary.", "sources": [second_ref]}],
            "topics": ["prices"],
            "entities": ["BLS"],
            "warnings": [],
        }
        return httpx.Response(
            200,
            json=_completion_payload(
                json.dumps(good), model="inclusionai/ling-3.2-flash:free", prompt_tokens=70, completion_tokens=30
            ),
        )

    transport, requests = _recording_transport(handler)
    enricher = OpenRouterScoutEnricher(
        eval_config=eval_config,
        prompt=prompt,
        ledger=ledger,
        experiment_id="exp-multi",
        transport=transport,
    )
    req = _scout_request(eval_config, prompt)

    with closing(open_database(tmp_path / "scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "artifacts", conn)
        telemetry = Telemetry(tmp_path / "telemetry.sqlite3")
        try:
            output = run_scout(
                req,
                store=store,
                telemetry=telemetry,
                fetch=lambda *_a, **_k: {
                    "a-first": first_item.canonical_bytes(),
                    "b-second": second_item.canonical_bytes(),
                },
                enrich=enricher,
            )
            spans = [
                span for _, span in telemetry.outbox.pending()
                if span.trace_id == output.trace_id and span.name == "model.call"
            ]
        finally:
            telemetry.shutdown(timeout_seconds=0)

    assert output.outcome == "partial"
    assert [(item.status, item.reason) for item in output.items] == [
        ("failed", "invalid_enrichment"),
        ("new", "candidate_created"),
    ]
    assert len(enricher.traces) == 2
    assert enricher.traces[0].failure_disposition == "foreign_reference"
    assert enricher.traces[0].quarantined is True
    assert enricher.traces[1].failure_disposition is None
    assert enricher.traces[1].resolved_model == "inclusionai/ling-3.2-flash:free"
    assert len(spans) == 2
    assert all(span.attributes.get("model.provider") == "openrouter" for span in spans)
    assert all(TEST_KEY.encode() not in span.canonical_bytes() for span in spans)


def test_public_eval_boundary_switches_profiles_and_preserves_shared_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    monkeypatch.delenv("FINANCE_ALLOW_PAID_PROVIDERS", raising=False)

    dev_examples = json.loads((DATASET / "dev.json").read_bytes())
    target_example = next(row for row in dev_examples if row["example_id"] == "release-periods")

    def make_handler(expected_model: str, resolved_model: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                assert f"/api/v1/models/{expected_model}/endpoints" in str(request.url)
                return httpx.Response(200, json=_endpoint_payload(model_id=expected_model))
            body = json.loads(request.content)
            assert body["model"] == expected_model
            user_payload = json.loads(body["messages"][1]["content"])
            members = user_payload["members"]
            enrichment = {
                "schema_version": "1.0",
                "summary": [
                    {
                        "text": "The latest synthetic price bulletin reported a rise.",
                        "sources": [members[0]["ref"]],
                    }
                ],
                "topics": ["prices"],
                "entities": ["Synthetic survey office"],
                "warnings": [],
            }
            return httpx.Response(
                200,
                json=_completion_payload(
                    json.dumps(enrichment),
                    model=resolved_model,
                    prompt_tokens=60,
                    completion_tokens=25,
                    cost=0.0,
                ),
            )
        return handler

    ling_transport, ling_reqs = _recording_transport(
        make_handler("inclusionai/ling-3.2-flash:free", "inclusionai/ling-3.2-flash:free")
    )
    ling_res = run_scout_eval(
        dataset=DATASET,
        split="dev",
        output_dir=tmp_path / "run-ling",
        ledger_db_path=tmp_path / "shared-ledger.sqlite3",
        experiment_id="shared-exp-switch",
        profile_name="ling",
        example_ids=("release-periods",),
        transport=ling_transport,
    )

    ernie_transport, ernie_reqs = _recording_transport(
        make_handler("baidu/ernie-5.5-flash-preview:free", "baidu/ernie-5.5-flash-preview:free")
    )
    ernie_res = run_scout_eval(
        dataset=DATASET,
        split="dev",
        output_dir=tmp_path / "run-ernie",
        ledger_db_path=tmp_path / "shared-ledger.sqlite3",
        experiment_id="shared-exp-switch",
        profile_name="ernie",
        example_ids=("release-periods",),
        transport=ernie_transport,
    )

    assert ling_res.requested_model == "inclusionai/ling-3.2-flash:free"
    assert ernie_res.requested_model == "baidu/ernie-5.5-flash-preview:free"
    assert ling_res.config_hash != ernie_res.config_hash
    # release-periods has 2 distinct URLs -> 2 clusters -> 2 preflight + 2 completions = 4 HTTP calls each.
    assert ling_res.outbound_call_count == len(ling_reqs) == 4
    assert ernie_res.outbound_call_count == len(ernie_reqs) == 4
    assert ernie_res.ledger_summary.reconciled_reservations == 4
    assert ernie_res.pending_reservations == 0
    assert ernie_res.cost == Decimal("0")

    # SavedExperiment produced by runner is valid for the offline Scout evaluator.
    saved_path = tmp_path / "run-ernie" / "saved_experiment.json"
    saved_path.write_bytes(ernie_res.saved_experiment.canonical_bytes())
    eval_report = evaluate_experiment(DATASET, saved_path)
    assert eval_report["model_id"] == "baidu/ernie-5.5-flash-preview:free"


def test_public_eval_boundary_quarantines_all_failure_modes_and_retains_ambiguous_on_timeout(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    toml_path = tmp_path / "paid.toml"
    toml_path.write_text(
        """
schema_version = 1
default_profile = "paid"
hard_spend_cap_usd = "0.05"

[profiles.paid]
model_id = "example/paid-eval"
input_usd_per_million_tokens = "0.10"
output_usd_per_million_tokens = "0.20"
""",
        encoding="utf-8",
    )

    # Select 5 dev examples with candidate items to exercise 5 failure modes + 1 clean example.
    examples = ("release-periods", "wire-overlap", "corrected-release", "conflicting-measures", "unsafe-feed-text", "quiet-day")
    completion_step = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal completion_step
        if request.method == "GET":
            # On the 5th completion attempt's preflight, return unavailable endpoint.
            if completion_step == 4:
                return httpx.Response(200, json=_endpoint_payload(model_id="example/paid-eval", status=-1))
            return httpx.Response(
                200,
                json=_endpoint_payload(
                    model_id="example/paid-eval",
                    prompt_price="0.0000001",
                    completion_price="0.0000002",
                ),
            )
        completion_step += 1
        if completion_step == 1:
            # 1. Invalid JSON
            return httpx.Response(200, json=_completion_payload("{not-json", model="example/paid-eval", cost=0.0001))
        if completion_step == 2:
            # 2. Extra field
            body = json.loads(request.content)
            ref = json.loads(body["messages"][1]["content"])["members"][0]["ref"]
            extra = {
                "schema_version": "1.0",
                "summary": [{"text": "Extra field output", "sources": [ref]}],
                "topics": ["prices"],
                "entities": ["Synth"],
                "warnings": [],
                "unexpected_extra": "bad",
            }
            return httpx.Response(200, json=_completion_payload(json.dumps(extra), model="example/paid-eval", cost=0.0001))
        if completion_step == 3:
            # 3. Foreign ref
            foreign = {
                "schema_version": "1.0",
                "summary": [{"text": "Foreign ref", "sources": [{
                    "source_id": str(uuid4()), "source_revision": 1, "source_record_hash": "a" * 64,
                }]}],
                "topics": ["prices"],
                "entities": ["Synth"],
                "warnings": [],
            }
            return httpx.Response(200, json=_completion_payload(json.dumps(foreign), model="example/paid-eval", cost=0.0001))
        if completion_step == 4:
            # 4. Timeout -> must leave reservation as 'ambiguous'
            raise httpx.ReadTimeout("timed out", request=request)
        # Subsequent clusters succeed.
        body = json.loads(request.content)
        ref = json.loads(body["messages"][1]["content"])["members"][0]["ref"]
        valid = {
            "schema_version": "1.0",
            "summary": [{"text": "Valid synthetic observation.", "sources": [ref]}],
            "topics": ["prices"],
            "entities": ["Synth"],
            "warnings": [],
        }
        return httpx.Response(200, json=_completion_payload(json.dumps(valid), model="example/paid-eval", cost=0.0001))

    transport, _ = _recording_transport(handler)
    result = run_scout_eval(
        dataset=DATASET,
        split="dev",
        output_dir=tmp_path / "eval-out",
        ledger_db_path=tmp_path / "ledger.sqlite3",
        experiment_id="exp-failures",
        profile_name="paid",
        config_path=toml_path,
        allow_paid_providers=True,
        example_ids=examples,
        transport=transport,
    )

    dispositions = {call.failure_disposition for call in result.calls if call.failure_disposition}
    assert {"invalid_json", "invalid_enrichment", "foreign_reference", "timeout", "endpoint_unavailable"} <= dispositions
    assert result.ambiguous_reservations >= 1
    assert result.ledger_summary.released_reservations >= 1
    assert result.ledger_summary.reconciled_reservations >= 3
    assert TEST_KEY not in result.model_dump_json()


def test_paid_profile_under_zero_cap_blocks_before_http_and_exhaustion_continues_eligible(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    zero_cap_paid = tmp_path / "zero-paid.toml"
    zero_cap_paid.write_text(
        """
schema_version = 1
default_profile = "paid"
hard_spend_cap_usd = "0.00"

[profiles.paid]
model_id = "example/paid-zero"
input_usd_per_million_tokens = "0.10"
output_usd_per_million_tokens = "0.20"
""",
        encoding="utf-8",
    )
    transport, requests = _recording_transport(lambda _req: httpx.Response(200, json={}))
    zero_res = run_scout_eval(
        dataset=DATASET,
        split="dev",
        output_dir=tmp_path / "zero-out",
        ledger_db_path=tmp_path / "zero-ledger.sqlite3",
        experiment_id="exp-zero-paid",
        profile_name="paid",
        config_path=zero_cap_paid,
        allow_paid_providers=True,
        example_ids=("corrected-release", "quiet-day"),
        transport=transport,
    )
    assert requests == []
    assert zero_res.outbound_call_count == 0
    assert zero_res.calls[0].failure_disposition == "spend_cap_exceeded"
    assert zero_res.calls[0].quarantined is True
    assert [ex.outcome for ex in zero_res.examples] == ["empty", "partial"]

    # Tight cap: first cluster exhausts cap, second cluster (larger max_tokens or second example) is quarantined,
    # while zero-LLM-call example ('quiet-day') still completes cleanly.
    tight_cap_paid = tmp_path / "tight-paid.toml"
    tight_cap_paid.write_text(
        """
schema_version = 1
default_profile = "paid"
hard_spend_cap_usd = "0.0003"

[profiles.paid]
model_id = "example/paid-tight"
input_usd_per_million_tokens = "0.10"
output_usd_per_million_tokens = "0.20"
""",
        encoding="utf-8",
    )

    def tight_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=_endpoint_payload(
                    model_id="example/paid-tight",
                    prompt_price="0.0000001",
                    completion_price="0.0000002",
                ),
            )
        body = json.loads(request.content)
        ref = json.loads(body["messages"][1]["content"])["members"][0]["ref"]
        valid = {
            "schema_version": "1.0",
            "summary": [{"text": "Valid synthetic observation.", "sources": [ref]}],
            "topics": ["prices"],
            "entities": ["Synth"],
            "warnings": [],
        }
        return httpx.Response(200, json=_completion_payload(json.dumps(valid), model="example/paid-tight", cost=0.00025))

    tight_transport, tight_reqs = _recording_transport(tight_handler)
    tight_res = run_scout_eval(
        dataset=DATASET,
        split="dev",
        output_dir=tmp_path / "tight-out",
        ledger_db_path=tmp_path / "tight-ledger.sqlite3",
        experiment_id="exp-tight-paid",
        profile_name="paid",
        config_path=tight_cap_paid,
        allow_paid_providers=True,
        example_ids=("quiet-day", "release-periods"),
        max_tokens=1500,
        transport=tight_transport,
    )
    # release-periods has 2 clusters; worst-case per cluster at max_tokens=1500 is > $0.00015 each,
    # so cluster 1 succeeds ($0.00025 actual) and cluster 2 is rejected by spend cap!
    assert tight_res.outbound_call_count == len(tight_reqs) == 2
    assert any(c.failure_disposition == "spend_cap_exceeded" for c in tight_res.calls)
    assert tight_res.examples[0].example_id == "quiet-day"
    assert tight_res.examples[0].outcome == "empty"
    assert tight_res.examples[1].example_id == "release-periods"
    assert tight_res.examples[1].outcome == "partial"


def test_duplicate_json_keys_and_missing_usage_without_cost_quarantine_and_mark_ambiguous(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    eval_config = load_scout_eval_config()
    prompt = load_prompt(SCOUT_ENRICHMENT_PROMPT_MANIFEST)
    recording = json.loads(RECORDING.read_bytes())
    item = ScoutFeedItem.model_validate_json(json.dumps(next(iter(recording["feed"]["items"].values()))))
    item_ref = json.dumps({
        "source_id": str(item.source.source_id),
        "source_revision": item.source.revision,
        "source_record_hash": item.source.record_hash(),
    })

    # 1. Duplicate JSON keys must be quarantined by BOTH call_trace and run_scout (0 candidates created).
    dup_json = (
        '{"schema_version":"1.0","schema_version":"1.0",'
        '"summary":[{"text":"Duplicate key payload.","sources":[' + item_ref + ']}],'
        '"topics":["prices"],"entities":["BLS"],"warnings":[]}'
    )

    def dup_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_endpoint_payload(model_id=eval_config.model_id))
        return httpx.Response(200, json=_completion_payload(dup_json))

    dup_db = tmp_path / "dup-ledger.sqlite3"
    with closing(open_database(dup_db)) as conn:
        migrate(conn)
    dup_transport, _ = _recording_transport(dup_handler)
    dup_enricher = OpenRouterScoutEnricher(
        eval_config=eval_config,
        prompt=prompt,
        ledger=SpendLedger(dup_db),
        experiment_id="exp-dup-keys",
        transport=dup_transport,
    )
    req = _scout_request(eval_config, prompt)
    with closing(open_database(tmp_path / "dup-scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "dup-artifacts", conn)
        telemetry = Telemetry(tmp_path / "dup-telemetry.sqlite3")
        try:
            dup_out = run_scout(
                req,
                store=store,
                telemetry=telemetry,
                fetch=lambda *_a, **_k: {"bls": item.canonical_bytes()},
                enrich=dup_enricher,
            )
        finally:
            telemetry.shutdown(timeout_seconds=0)

    assert dup_out.outcome == "failed"
    assert dup_out.candidates == ()
    assert [(i.status, i.reason) for i in dup_out.items] == [("failed", "invalid_enrichment")]
    assert dup_enricher.traces[0].failure_disposition == "invalid_json"
    assert dup_enricher.traces[0].quarantined is True

    # 2. Missing usage without reported cost must retain reservation as 'ambiguous' (never 'reconciled').
    paid_toml = tmp_path / "paid-missing-usage.toml"
    paid_toml.write_text(
        """
schema_version = 1
default_profile = "paid"
hard_spend_cap_usd = "0.05"

[profiles.paid]
model_id = "example/paid-missing-usage"
input_usd_per_million_tokens = "0.10"
output_usd_per_million_tokens = "0.20"
""",
        encoding="utf-8",
    )
    paid_cfg = load_scout_eval_config(config_path=paid_toml, allow_paid_providers=True)

    def missing_usage_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=_endpoint_payload(
                    model_id="example/paid-missing-usage",
                    prompt_price="0.0000001",
                    completion_price="0.0000002",
                ),
            )
        return httpx.Response(
            200,
            json={
                "model": "example/paid-missing-usage",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            },
        )

    miss_db = tmp_path / "miss-ledger.sqlite3"
    with closing(open_database(miss_db)) as conn:
        migrate(conn)
    miss_ledger = SpendLedger(miss_db)
    miss_transport, _ = _recording_transport(missing_usage_handler)
    miss_enricher = OpenRouterScoutEnricher(
        eval_config=paid_cfg,
        prompt=prompt,
        ledger=miss_ledger,
        experiment_id="exp-missing-usage",
        transport=miss_transport,
    )
    miss_req = _scout_request(paid_cfg, prompt)
    with closing(open_database(tmp_path / "miss-scout.sqlite3")) as conn:
        migrate(conn)
        store = ArtifactStore(tmp_path / "miss-artifacts", conn)
        telemetry = Telemetry(tmp_path / "miss-telemetry.sqlite3")
        try:
            miss_out = run_scout(
                miss_req,
                store=store,
                telemetry=telemetry,
                fetch=lambda *_a, **_k: {"bls": item.canonical_bytes()},
                enrich=miss_enricher,
            )
        finally:
            telemetry.shutdown(timeout_seconds=0)

    assert miss_out.outcome == "failed"
    summary = miss_ledger.summary("exp-missing-usage")
    assert summary.ambiguous_reservations == 1
    assert summary.reconciled_reservations == 0
    assert miss_enricher.traces[0].reservation_status == "ambiguous"
    assert miss_enricher.traces[0].cost is None
    assert miss_enricher.traces[0].failure_disposition == "missing_usage"

