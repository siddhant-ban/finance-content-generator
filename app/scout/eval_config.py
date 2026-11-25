"""Secret-free TOML profile catalog and resolved configuration for Scout OpenRouter evals."""
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import tomllib
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from app.contracts.common import Nonempty, Sha256, StrictModel

DEFAULT_SCOUT_EVAL_CONFIG_PATH = Path("config/scout-openrouter-eval.example.toml")
DEFAULT_SCOUT_EVAL_PROFILE = "ling"
DEFAULT_HARD_SPEND_CAP_USD = Decimal("0.00")

_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SECRET_PREFIXES = ("sk-", "ghp_", "gho_", "github_pat_", "token", "password")


def _parse_nonnegative_decimal(value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError("decimal values must be strings, integers, or Decimals")
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, (str, int)):
        try:
            dec = Decimal(str(value).strip())
        except InvalidOperation as error:
            raise ValueError("invalid decimal value") from error
    else:
        raise ValueError("decimal value required")
    if not dec.is_finite() or dec < 0:
        raise ValueError("decimal value must be finite and nonnegative")
    return dec


NonNegativeDecimal = Annotated[
    Decimal,
    BeforeValidator(_parse_nonnegative_decimal),
    Field(ge=0, allow_inf_nan=False),
]


def validate_openrouter_model_id(model_id: str) -> str:
    """Validate an exact OpenRouter model identifier without path traversal or credentials."""
    if not isinstance(model_id, str):
        raise ValueError("model_id must be a string")
    value = model_id.strip()
    if (
        not value
        or len(value) > 128
        or ".." in value
        or "://" in value
        or not _MODEL_ID_PATTERN.fullmatch(value)
        or any(part.startswith(_SECRET_PREFIXES) for part in value.lower().split("/"))
    ):
        raise ValueError("invalid_model_id")
    return value


class ScoutProfileSpec(StrictModel):
    """One named OpenRouter evaluation profile with pinned per-million token USD rates."""

    model_id: Nonempty
    input_usd_per_million_tokens: NonNegativeDecimal
    output_usd_per_million_tokens: NonNegativeDecimal

    @field_validator("model_id")
    @classmethod
    def _valid_model_id(cls, value: str) -> str:
        return validate_openrouter_model_id(value)


class ScoutEvalCatalog(StrictModel):
    """Versioned, secret-free TOML profile catalog for Scout OpenRouter evaluations."""

    schema_version: Literal[1]
    default_profile: Nonempty = DEFAULT_SCOUT_EVAL_PROFILE
    hard_spend_cap_usd: NonNegativeDecimal = DEFAULT_HARD_SPEND_CAP_USD
    profiles: dict[Nonempty, ScoutProfileSpec]

    @model_validator(mode="after")
    def _validate_profiles(self):
        if not self.profiles:
            raise ValueError("catalog requires at least one profile")
        if any(not key.strip() or key != key.strip() for key in self.profiles):
            raise ValueError("profile names must be non-empty without surrounding whitespace")
        if self.default_profile not in self.profiles:
            raise ValueError(f"unknown_profile: {self.default_profile}")
        return self

    @classmethod
    def from_toml_path(cls, path: Path) -> "ScoutEvalCatalog":
        """Load and strictly validate a secret-free TOML profile catalog."""
        if not isinstance(path, Path):
            raise TypeError("catalog path must be a Path")
        raw = tomllib.loads(path.read_bytes().decode("utf-8"))
        return cls.model_validate(raw)


class ResolvedScoutEvalConfig(StrictModel):
    """Resolved non-secret evaluation profile configuration with deterministic SHA-256 hash."""

    schema_version: Literal[1]
    profile_name: Nonempty
    model_id: Nonempty
    input_usd_per_million_tokens: NonNegativeDecimal
    output_usd_per_million_tokens: NonNegativeDecimal
    hard_spend_cap_usd: NonNegativeDecimal
    allow_paid_providers: bool
    catalog_sha256: Sha256

    def config_hash(self) -> str:
        """Return SHA-256 of the canonical resolved non-secret configuration."""
        return self.record_hash()

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable non-secret snapshot of the resolved configuration."""
        return self.model_dump(mode="json")


def _resolve_allow_paid(explicit: bool | None) -> bool:
    if explicit is not None:
        if type(explicit) is not bool:
            raise TypeError("allow_paid_providers must be a bool")
        return explicit
    raw = os.environ.get("FINANCE_ALLOW_PAID_PROVIDERS")
    if raw is None:
        return False
    lowered = raw.strip().lower()
    if lowered not in {"true", "false"}:
        raise ValueError("FINANCE_ALLOW_PAID_PROVIDERS must be true or false")
    return lowered == "true"


def get_openrouter_api_key() -> str:
    """Read OPENROUTER_API_KEY from environment only; never serialize or log it."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ValueError("openrouter_credential_missing")
    return key


def load_scout_eval_config(
    *,
    config_path: Path | None = None,
    profile_name: str | None = None,
    allow_paid_providers: bool | None = None,
) -> ResolvedScoutEvalConfig:
    """Load the Scout OpenRouter eval catalog, resolve the active profile, and enforce paid opt-in."""
    if config_path is None:
        env_path = os.environ.get("FINANCE_SCOUT_EVAL_CONFIG")
        if env_path is not None:
            stripped = env_path.strip()
            if not stripped:
                raise ValueError("invalid_config_path")
            resolved_path = Path(stripped)
        else:
            resolved_path = DEFAULT_SCOUT_EVAL_CONFIG_PATH
    else:
        resolved_path = config_path

    catalog = ScoutEvalCatalog.from_toml_path(resolved_path)

    if profile_name is None:
        env_profile = os.environ.get("FINANCE_SCOUT_EVAL_PROFILE")
        selected_profile = env_profile if env_profile is not None else catalog.default_profile
    else:
        selected_profile = profile_name

    if not selected_profile or selected_profile not in catalog.profiles:
        raise ValueError(f"unknown_profile: {selected_profile}")

    profile = catalog.profiles[selected_profile]
    paid_allowed = _resolve_allow_paid(allow_paid_providers)
    positive_spend = (
        catalog.hard_spend_cap_usd > Decimal("0")
        or profile.input_usd_per_million_tokens > Decimal("0")
        or profile.output_usd_per_million_tokens > Decimal("0")
    )
    if positive_spend and not paid_allowed:
        raise ValueError("paid_providers_not_authorized")

    return ResolvedScoutEvalConfig(
        schema_version=catalog.schema_version,
        profile_name=selected_profile,
        model_id=profile.model_id,
        input_usd_per_million_tokens=profile.input_usd_per_million_tokens,
        output_usd_per_million_tokens=profile.output_usd_per_million_tokens,
        hard_spend_cap_usd=catalog.hard_spend_cap_usd,
        allow_paid_providers=paid_allowed,
        catalog_sha256=catalog.record_hash(),
    )
