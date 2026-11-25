"""OpenRouter enrichment adapter for News Scout with pre-flight verification and spend ledger."""
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from opentelemetry import trace
from pydantic import Field, ValidationError

from app.contracts.common import Nonempty, StrictModel
from app.contracts.scout import (
    ScoutEnrichment,
    ScoutMember,
    ScoutRunInput,
    ScoutTokenUsage,
)
from app.observability.sanitize import sanitize_attributes
from app.prompts import PromptSpec
from app.scout.eval_config import (
    NonNegativeDecimal,
    ResolvedScoutEvalConfig,
    get_openrouter_api_key,
)
from app.scout.ledger import (
    ReservationStatus,
    SpendCapExceeded,
    SpendLedger,
    compute_cost_usd,
)
from app.scout.run import ModelResult, TokenBudgetExceeded

SCOUT_ENRICHMENT_PROMPT_MANIFEST = Path("prompts/manifests/scout_enrichment.v1.json")
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_COMPLETIONS_URL = f"{_OPENROUTER_BASE_URL}/chat/completions"


class PreFlightError(ValueError):
    """Pre-flight OpenRouter endpoint or pricing verification blocked execution before send."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class VerifiedEndpoint(StrictModel):
    """Verified endpoint metadata matching profile constraints."""

    model_id: Nonempty
    endpoint_name: Nonempty | None = None
    input_usd_per_million_tokens: NonNegativeDecimal
    output_usd_per_million_tokens: NonNegativeDecimal


class ScoutCallTrace(StrictModel):
    """Sanitized per-call trace for OpenRouter Scout enrichment."""

    example_id: Nonempty
    cluster_key: Nonempty
    reservation_id: Nonempty | None = None
    requested_model: Nonempty
    resolved_model: Nonempty | None = None
    parameters: dict[str, object] = Field(default_factory=dict)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: NonNegativeDecimal | None = None
    reserved_usd: NonNegativeDecimal = Decimal("0")
    reservation_status: ReservationStatus | Literal["not_reserved"] = "not_reserved"
    failure_disposition: Nonempty | None = None
    quarantined: bool = False


@dataclass(frozen=True)
class ClusterEnrichmentResult:
    """Rich enrichment outcome used by both run_scout and scout_runner."""

    model_result: ModelResult | None
    call_trace: ScoutCallTrace
    enrichment: ScoutEnrichment | None = None


def _strictify_schema_node(node: object) -> object:
    if isinstance(node, list):
        return [_strictify_schema_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: _strictify_schema_node(v) for k, v in node.items()}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


def strict_scout_enrichment_schema() -> dict[str, object]:
    """Return a strict JSON Schema for ScoutEnrichment compatible with OpenRouter structured_outputs."""
    raw = ScoutEnrichment.model_json_schema()
    result = _strictify_schema_node(raw)
    assert isinstance(result, dict)
    return result


SCOUT_ENRICHMENT_SCHEMA_SHA256 = hashlib.sha256(
    json.dumps(strict_scout_enrichment_schema(), sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def _parse_usd_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float, Decimal)):
        try:
            dec = Decimal(str(value).strip())
        except InvalidOperation:
            return None
        if not dec.is_finite() or dec < 0:
            return None
        return dec
    return None


def _check_endpoint_availability(endpoint: Mapping[str, object]) -> str | None:
    if "status" in endpoint:
        status = endpoint["status"]
        if isinstance(status, bool):
            if not status:
                return "endpoint_unavailable"
        elif isinstance(status, int):
            if status != 0:
                return "endpoint_unavailable"
        elif isinstance(status, str):
            if status.strip().lower() not in {"0", "active", "available", "ok"}:
                return "endpoint_unavailable"
        else:
            return "endpoint_unavailable"
    return None


def _check_endpoint_data_use(endpoint: Mapping[str, object]) -> str | None:
    if "permitted_data_use" in endpoint and endpoint["permitted_data_use"] is not True:
        return "data_policy_denied"
    if "data_collection" in endpoint and endpoint["data_collection"] not in ("deny", False):
        return "data_policy_denied"
    if "zdr" in endpoint and endpoint["zdr"] is not True:
        return "data_policy_denied"
    if "data_policy" in endpoint:
        policy = endpoint["data_policy"]
        if not isinstance(policy, Mapping):
            return "data_policy_denied"
        if policy.get("trains_on_prompts") is True or policy.get("retains_prompts") is True:
            return "data_policy_denied"
        if "data_collection" in policy and policy["data_collection"] != "deny":
            return "data_policy_denied"
        if "permitted" in policy and policy["permitted"] is not True:
            return "data_policy_denied"
    return None


def _check_endpoint_pricing(
    endpoint: Mapping[str, object],
    eval_config: ResolvedScoutEvalConfig,
) -> tuple[str | None, Decimal, Decimal]:
    pricing = endpoint.get("pricing")
    if not isinstance(pricing, Mapping):
        return "invalid_pricing", Decimal("0"), Decimal("0")

    prompt_per_token = _parse_usd_decimal(pricing.get("prompt"))
    completion_per_token = _parse_usd_decimal(pricing.get("completion"))
    if prompt_per_token is None or completion_per_token is None:
        return "invalid_pricing", Decimal("0"), Decimal("0")

    input_per_million = prompt_per_token * Decimal("1000000")
    output_per_million = completion_per_token * Decimal("1000000")
    if (
        input_per_million > eval_config.input_usd_per_million_tokens
        or output_per_million > eval_config.output_usd_per_million_tokens
    ):
        return "rate_exceeds_profile", input_per_million, output_per_million

    for key, value in pricing.items():
        if key in {"prompt", "completion"} or value is None:
            continue
        extra = _parse_usd_decimal(value)
        if extra is None:
            return "invalid_pricing", input_per_million, output_per_million
        if extra != Decimal("0"):
            return "extra_fee_rejected", input_per_million, output_per_million

    return None, input_per_million, output_per_million


def verify_openrouter_endpoint(
    eval_config: ResolvedScoutEvalConfig,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = 10.0,
) -> VerifiedEndpoint:
    """Query OpenRouter endpoint metadata and enforce availability, structured_outputs, privacy, and rates."""
    url = f"{_OPENROUTER_BASE_URL}/models/{eval_config.model_id}/endpoints"
    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.get(url)
            if response.status_code != 200:
                raise PreFlightError("endpoint_unavailable")
            payload = response.json()
    except PreFlightError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        raise PreFlightError("endpoint_unavailable") from None

    if not isinstance(payload, Mapping):
        raise PreFlightError("endpoint_unavailable")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise PreFlightError("endpoint_unavailable")
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise PreFlightError("endpoint_unavailable")

    last_reason = "endpoint_unavailable"
    for endpoint in endpoints:
        if not isinstance(endpoint, Mapping):
            last_reason = "endpoint_unavailable"
            continue
        avail_err = _check_endpoint_availability(endpoint)
        if avail_err:
            last_reason = avail_err
            continue
        supported = endpoint.get("supported_parameters")
        if not isinstance(supported, list) or "structured_outputs" not in supported:
            last_reason = "structured_outputs_unsupported"
            continue
        data_err = _check_endpoint_data_use(endpoint)
        if data_err:
            last_reason = data_err
            continue
        price_err, in_m, out_m = _check_endpoint_pricing(endpoint, eval_config)
        if price_err:
            last_reason = price_err
            continue
        name = endpoint.get("name")
        return VerifiedEndpoint(
            model_id=eval_config.model_id,
            endpoint_name=name if isinstance(name, str) and name.strip() else None,
            input_usd_per_million_tokens=in_m,
            output_usd_per_million_tokens=out_m,
        )

    raise PreFlightError(last_reason)


def _strict_json_loads(raw_text: str) -> object:
    def _reject_constant(token: str) -> object:
        raise ValueError(f"nonfinite_json_constant: {token}")

    def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for k, v in pairs:
            if k in result:
                raise ValueError(f"duplicate_json_key: {k}")
            result[k] = v
        return result

    return json.loads(raw_text, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)


def _validate_enrichment_payload(
    content_text: str,
    members: tuple[ScoutMember, ...],
) -> tuple[ScoutEnrichment | None, str | None]:
    try:
        _strict_json_loads(content_text)
    except (ValueError, TypeError, RecursionError):
        return None, "invalid_json"
    try:
        enrichment = ScoutEnrichment.model_validate_json(content_text)
    except ValidationError:
        return None, "invalid_enrichment"

    member_refs = {member.ref.canonical_bytes() for member in members}
    for statement in enrichment.summary + enrichment.uncertainty:
        for ref in statement.sources:
            if ref.canonical_bytes() not in member_refs:
                return None, "foreign_reference"
    return enrichment, None


def _extract_usage(
    payload: Mapping[str, object],
) -> tuple[int | None, int | None, Decimal | None]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None, None, None
    in_tok = usage.get("prompt_tokens")
    out_tok = usage.get("completion_tokens")
    in_val = in_tok if type(in_tok) is int and 0 <= in_tok < 2**63 else None
    out_val = out_tok if type(out_tok) is int and 0 <= out_tok < 2**63 else None
    cost_val = _parse_usd_decimal(usage.get("cost"))
    return in_val, out_val, cost_val


def _safe_resolved_model(raw_model: object, api_key: str) -> str | None:
    safe_model = sanitize_attributes({"model.resolved_name": raw_model}).get("model.resolved_name")
    if isinstance(safe_model, str) and api_key and api_key in safe_model:
        return None
    return safe_model if isinstance(safe_model, str) else None


def _cluster_user_payload(members: tuple[ScoutMember, ...]) -> bytes:
    payload = {
        "schema_version": "1.0",
        "members": [
            {
                "headline": member.headline,
                "ref": member.ref.model_dump(mode="json"),
                "source": {
                    "publisher": member.source.publisher,
                    "source_class": member.source.source_class,
                    "canonical_url": member.source.canonical_url,
                    "published_at": member.source.published_at.isoformat() if member.source.published_at else None,
                    "retrieved_at": member.source.retrieved_at.isoformat(),
                    "locator": member.source.locator,
                    "excerpt": member.source.excerpt,
                    "rights": member.source.rights,
                },
            }
            for member in members
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class OpenRouterScoutEnricher:
    """Enricher adapter injecting pinned OpenRouter calls with pre-flight and ledger enforcement."""

    def __init__(
        self,
        *,
        eval_config: ResolvedScoutEvalConfig,
        prompt: PromptSpec,
        ledger: SpendLedger,
        experiment_id: str,
        prompt_manifest_path: Path = SCOUT_ENRICHMENT_PROMPT_MANIFEST,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if prompt.prompt_id != "scout_enrichment" or prompt.prompt_version != "scout_enrichment.v1":
            raise ValueError("scout_prompt_mismatch")
        self.eval_config = eval_config
        self.prompt = prompt
        self.ledger = ledger
        self.experiment_id = experiment_id
        self._manifest_path = prompt_manifest_path.resolve()
        self._prompt_path = (self._manifest_path.parent / prompt.path).resolve()
        self._transport = transport
        self._attempt_seq = 0
        self.outbound_call_count = 0
        self.preflight_call_count = 0
        self.completion_call_count = 0
        self.traces: list[ScoutCallTrace] = []

        self.ledger.ensure_experiment(
            experiment_id,
            hard_spend_cap_usd=eval_config.hard_spend_cap_usd,
            config_hash=eval_config.config_hash(),
        )

    def _read_verified_prompt(self) -> bytes:
        prompt_root = self._manifest_path.parent.parent.resolve()
        if not self._prompt_path.is_relative_to(prompt_root):
            raise ValueError("scout_prompt_mismatch")
        prompt_bytes = self._prompt_path.read_bytes()
        if hashlib.sha256(prompt_bytes).hexdigest() != self.prompt.sha256:
            raise ValueError("scout_prompt_mismatch")
        return prompt_bytes

    def _validate_request_pins(self, request: ScoutRunInput) -> None:
        cfg = request.configuration
        if (
            cfg.provider != "openrouter"
            or cfg.model != self.eval_config.model_id
            or cfg.prompt_version != self.prompt.prompt_version
            or cfg.prompt_hash != self.prompt.sha256
        ):
            raise ValueError("scout_configuration_pin_mismatch")

    def _set_span_attributes(
        self,
        *,
        resolved_model: str | None,
        parameters: Mapping[str, object],
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: Decimal | None,
        error_code: str | None = None,
    ) -> None:
        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        attrs: dict[str, object] = {
            "model.provider": "openrouter",
            "model.name": self.eval_config.model_id,
            "model.parameters": dict(parameters),
            "prompt.version": self.prompt.prompt_version,
            "prompt.hash": self.prompt.sha256,
            "config.hash": self.eval_config.config_hash(),
        }
        if resolved_model is not None:
            attrs["model.resolved_name"] = resolved_model
        if input_tokens is not None:
            attrs["usage.input_tokens"] = input_tokens
        if output_tokens is not None:
            attrs["usage.output_tokens"] = output_tokens
        if cost_usd is not None:
            attrs["usage.measured_cost"] = float(cost_usd)
            attrs["usage.currency"] = "USD"
        if error_code is not None:
            attrs["error.code"] = error_code
        span.set_attributes(sanitize_attributes(attrs))

    def _record_trace(self, call_trace: ScoutCallTrace) -> None:
        self.traces.append(call_trace)

    def enrich_cluster(
        self,
        request: ScoutRunInput,
        members: tuple[ScoutMember, ...],
        *,
        max_tokens: int,
        timeout_seconds: float,
        example_id: str = "scout_run",
        cluster_key: str | None = None,
    ) -> ClusterEnrichmentResult:
        """Execute pre-flight, atomic reservation, constrained completion, and reconciliation."""
        self._attempt_seq += 1
        key = cluster_key or members[0].source.canonical_url
        attempt_key = f"{example_id}:{self.eval_config.profile_name}:{self._attempt_seq}:{uuid4().hex[:8]}"
        params: dict[str, object] = {
            "temperature": request.configuration.temperature,
            "max_tokens": max_tokens,
        }

        try:
            self._validate_request_pins(request)
            prompt_bytes = self._read_verified_prompt()
            api_key = get_openrouter_api_key()
        except ValueError:
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    failure_disposition="configuration_or_credential_invalid",
                    quarantined=True,
                )
            )
            raise

        user_bytes = _cluster_user_payload(members)
        estimated_input_tokens = math.ceil((len(prompt_bytes) + len(user_bytes)) / 3) + 32
        if estimated_input_tokens >= max_tokens:
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    failure_disposition="token_limit",
                    quarantined=True,
                )
            )
            raise TokenBudgetExceeded("request exceeds max_tokens ceiling")

        max_output_tokens = max_tokens - estimated_input_tokens
        params["max_tokens"] = max_output_tokens
        worst_case_usd = compute_cost_usd(
            input_tokens=estimated_input_tokens,
            output_tokens=max_output_tokens,
            input_usd_per_million_tokens=self.eval_config.input_usd_per_million_tokens,
            output_usd_per_million_tokens=self.eval_config.output_usd_per_million_tokens,
        )

        try:
            reservation = self.ledger.reserve(
                experiment_id=self.experiment_id,
                profile_name=self.eval_config.profile_name,
                model_id=self.eval_config.model_id,
                example_id=example_id,
                attempt_key=attempt_key,
                worst_case_usd=worst_case_usd,
            )
        except SpendCapExceeded:
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    failure_disposition="spend_cap_exceeded",
                    quarantined=True,
                )
            )
            self._set_span_attributes(
                resolved_model=None,
                parameters=params,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                error_code="capacity_exceeded",
            )
            raise

        # Pre-flight verification runs before sending completion; any failure releases reservation.
        self.preflight_call_count += 1
        self.outbound_call_count += 1
        try:
            verify_openrouter_endpoint(
                self.eval_config,
                transport=self._transport,
                timeout_seconds=timeout_seconds,
            )
        except PreFlightError as err:
            released = self.ledger.release(reservation.reservation_id, failure_disposition=err.reason)
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=released.reservation_id,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    reserved_usd=released.reserved_usd,
                    reservation_status=released.status,
                    failure_disposition=err.reason,
                    quarantined=True,
                )
            )
            self._set_span_attributes(
                resolved_model=None,
                parameters=params,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                error_code="unavailable",
            )
            raise OSError(f"preflight_blocked:{err.reason}") from None

        request_body = {
            "model": self.eval_config.model_id,
            "messages": [
                {"role": "system", "content": prompt_bytes.decode("utf-8")},
                {"role": "user", "content": user_bytes.decode("utf-8")},
            ],
            "max_tokens": max_output_tokens,
            "temperature": request.configuration.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ScoutEnrichment",
                    "strict": True,
                    "schema": strict_scout_enrichment_schema(),
                },
            },
            "provider": {
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "max_price": {
                    "prompt": float(self.eval_config.input_usd_per_million_tokens),
                    "completion": float(self.eval_config.output_usd_per_million_tokens),
                },
            },
        }

        self.completion_call_count += 1
        self.outbound_call_count += 1
        try:
            with httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = client.post(
                    _COMPLETIONS_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=request_body,
                )
        except httpx.TimeoutException:
            ambig = self.ledger.mark_ambiguous(reservation.reservation_id, failure_disposition="timeout")
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=ambig.reservation_id,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    reserved_usd=ambig.reserved_usd,
                    reservation_status=ambig.status,
                    failure_disposition="timeout",
                    quarantined=True,
                )
            )
            self._set_span_attributes(
                resolved_model=None,
                parameters=params,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                error_code="timeout",
            )
            raise TimeoutError("openrouter_timeout") from None
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            ambig = self.ledger.mark_ambiguous(reservation.reservation_id, failure_disposition="transport_error")
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=ambig.reservation_id,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    reserved_usd=ambig.reserved_usd,
                    reservation_status=ambig.status,
                    failure_disposition="transport_error",
                    quarantined=True,
                )
            )
            self._set_span_attributes(
                resolved_model=None,
                parameters=params,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                error_code="unavailable",
            )
            raise OSError("openrouter_request_failed") from None

        if response.status_code != 200:
            ambig = self.ledger.mark_ambiguous(reservation.reservation_id, failure_disposition="http_error")
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=ambig.reservation_id,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    reserved_usd=ambig.reserved_usd,
                    reservation_status=ambig.status,
                    failure_disposition="http_error",
                    quarantined=True,
                )
            )
            self._set_span_attributes(
                resolved_model=None,
                parameters=params,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                error_code="unavailable",
            )
            raise OSError("openrouter_http_error")

        try:
            payload = response.json()
        except ValueError:
            ambig = self.ledger.mark_ambiguous(reservation.reservation_id, failure_disposition="invalid_response")
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=ambig.reservation_id,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    reserved_usd=ambig.reserved_usd,
                    reservation_status=ambig.status,
                    failure_disposition="invalid_response",
                    quarantined=True,
                )
            )
            raise ValueError("invalid_provider_completion") from None

        if not isinstance(payload, Mapping) or "error" in payload:
            ambig = self.ledger.mark_ambiguous(reservation.reservation_id, failure_disposition="provider_error")
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=ambig.reservation_id,
                    requested_model=self.eval_config.model_id,
                    parameters=params,
                    reserved_usd=ambig.reserved_usd,
                    reservation_status=ambig.status,
                    failure_disposition="provider_error",
                    quarantined=True,
                )
            )
            raise ValueError("provider_error_response")

        in_tok, out_tok, reported_cost = _extract_usage(payload)
        safe_model = _safe_resolved_model(payload.get("model"), api_key)

        try:
            choices = payload["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("unexpected_choices")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("incomplete_finish")
            content_text = choice["message"]["content"]
            if not isinstance(content_text, str):
                raise ValueError("non_string_content")
        except (KeyError, TypeError, AttributeError, ValueError):
            if reported_cost is not None or (in_tok is not None and out_tok is not None):
                actual = (
                    reported_cost
                    if reported_cost is not None
                    else compute_cost_usd(
                        input_tokens=in_tok or 0,
                        output_tokens=out_tok or 0,
                        input_usd_per_million_tokens=self.eval_config.input_usd_per_million_tokens,
                        output_usd_per_million_tokens=self.eval_config.output_usd_per_million_tokens,
                    )
                )
                rec = self.ledger.reconcile(
                    reservation.reservation_id,
                    actual_usd=actual,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    resolved_model=safe_model,
                    failure_disposition="invalid_response",
                )
                status_val = rec.status
                cost_val = rec.charged_usd
            else:
                rec = self.ledger.mark_ambiguous(reservation.reservation_id, failure_disposition="invalid_response")
                status_val = rec.status
                cost_val = None
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=rec.reservation_id,
                    requested_model=self.eval_config.model_id,
                    resolved_model=safe_model,
                    parameters=params,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost=cost_val,
                    reserved_usd=rec.reserved_usd,
                    reservation_status=status_val,
                    failure_disposition="invalid_response",
                    quarantined=True,
                )
            )
            raise ValueError("invalid_provider_completion") from None

        if in_tok is None or out_tok is None:
            if reported_cost is not None:
                rec = self.ledger.reconcile(
                    reservation.reservation_id,
                    actual_usd=reported_cost,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    resolved_model=safe_model,
                    failure_disposition="missing_usage",
                )
                cost_val = rec.charged_usd
            else:
                rec = self.ledger.mark_ambiguous(
                    reservation.reservation_id,
                    failure_disposition="missing_usage",
                )
                cost_val = None
            self._record_trace(
                ScoutCallTrace(
                    example_id=example_id,
                    cluster_key=key,
                    reservation_id=rec.reservation_id,
                    requested_model=self.eval_config.model_id,
                    resolved_model=safe_model,
                    parameters=params,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost=cost_val,
                    reserved_usd=rec.reserved_usd,
                    reservation_status=rec.status,
                    failure_disposition="missing_usage",
                    quarantined=True,
                )
            )
            raise ValueError("missing_usage_tokens")

        actual_usd = (
            reported_cost
            if reported_cost is not None
            else compute_cost_usd(
                input_tokens=in_tok,
                output_tokens=out_tok,
                input_usd_per_million_tokens=self.eval_config.input_usd_per_million_tokens,
                output_usd_per_million_tokens=self.eval_config.output_usd_per_million_tokens,
            )
        )

        enrichment, validation_err = _validate_enrichment_payload(content_text, members)
        if in_tok + out_tok > max_tokens:
            failure_disp = "model_token_overrun"
        else:
            failure_disp = validation_err

        reconciled = self.ledger.reconcile(
            reservation.reservation_id,
            actual_usd=actual_usd,
            input_tokens=in_tok,
            output_tokens=out_tok,
            resolved_model=safe_model,
            failure_disposition=failure_disp,
        )
        self._set_span_attributes(
            resolved_model=safe_model,
            parameters=params,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=reconciled.charged_usd,
            error_code="invalid_record" if failure_disp else None,
        )
        call_trace = ScoutCallTrace(
            example_id=example_id,
            cluster_key=key,
            reservation_id=reconciled.reservation_id,
            requested_model=self.eval_config.model_id,
            resolved_model=safe_model,
            parameters=params,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost=reconciled.charged_usd,
            reserved_usd=reconciled.reserved_usd,
            reservation_status=reconciled.status,
            failure_disposition=failure_disp,
            quarantined=failure_disp is not None,
        )
        self._record_trace(call_trace)

        # Return empty bytes whenever validation_err is set so run_scout quarantines
        # even subtle strict-JSON failures (such as duplicate keys) as invalid_enrichment.
        usage_model = ScoutTokenUsage(input_tokens=in_tok, output_tokens=out_tok)
        return ClusterEnrichmentResult(
            model_result=ModelResult(
                output=content_text.encode("utf-8") if validation_err is None else b"",
                usage=usage_model,
            ),
            call_trace=call_trace,
            enrichment=enrichment if failure_disp is None else None,
        )

    def __call__(
        self,
        request: ScoutRunInput,
        members: tuple[ScoutMember, ...],
        *,
        max_tokens: int,
        timeout_seconds: float,
    ) -> ModelResult:
        """Implement the Scout Enricher protocol for run_scout injection."""
        result = self.enrich_cluster(
            request,
            members,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
        assert result.model_result is not None
        return result.model_result
