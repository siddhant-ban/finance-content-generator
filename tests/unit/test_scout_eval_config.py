"""Unit tests for Scout OpenRouter evaluation profile catalog and config."""
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.scout.eval_config import (
    DEFAULT_SCOUT_EVAL_CONFIG_PATH,
    DEFAULT_SCOUT_EVAL_PROFILE,
    ScoutEvalCatalog,
    get_openrouter_api_key,
    load_scout_eval_config,
)


def _write_toml(tmp_path: Path, body: str, name: str = "scout-eval.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_checked_in_example_catalog_with_safe_defaults(monkeypatch):
    monkeypatch.delenv("FINANCE_SCOUT_EVAL_PROFILE", raising=False)
    monkeypatch.delenv("FINANCE_SCOUT_EVAL_CONFIG", raising=False)
    monkeypatch.delenv("FINANCE_ALLOW_PAID_PROVIDERS", raising=False)

    resolved = load_scout_eval_config()

    assert DEFAULT_SCOUT_EVAL_PROFILE == "ling"
    assert DEFAULT_SCOUT_EVAL_CONFIG_PATH == Path("config/scout-openrouter-eval.example.toml")
    assert resolved.schema_version == 1
    assert resolved.profile_name == "ling"
    assert resolved.model_id == "inclusionai/ling-3.2-flash:free"
    assert resolved.input_usd_per_million_tokens == Decimal("0")
    assert resolved.output_usd_per_million_tokens == Decimal("0")
    assert resolved.hard_spend_cap_usd == Decimal("0.00")
    assert resolved.allow_paid_providers is False
    assert len(resolved.config_hash()) == 64


def test_env_selects_profile_and_changes_config_hash(monkeypatch):
    monkeypatch.delenv("FINANCE_ALLOW_PAID_PROVIDERS", raising=False)
    monkeypatch.setenv("FINANCE_SCOUT_EVAL_PROFILE", "ling")
    ling = load_scout_eval_config()

    monkeypatch.setenv("FINANCE_SCOUT_EVAL_PROFILE", "ernie")
    ernie = load_scout_eval_config()

    assert ernie.profile_name == "ernie"
    assert ernie.model_id == "baidu/ernie-5.5-flash-preview:free"
    assert ling.config_hash() != ernie.config_hash()
    assert ling.snapshot() != ernie.snapshot()


def test_openrouter_api_key_is_env_only_and_never_serialized_or_hashed(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="openrouter_credential_missing"):
        get_openrouter_api_key()

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret-one")
    first = load_scout_eval_config()
    assert get_openrouter_api_key() == "sk-or-v1-secret-one"
    assert "sk-or-v1-secret-one" not in repr(first)
    assert "sk-or-v1-secret-one" not in first.model_dump_json()
    assert "sk-or-v1-secret-one" not in str(first.snapshot())

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret-two")
    second = load_scout_eval_config()
    assert first.config_hash() == second.config_hash()


def test_unknown_profile_and_invalid_catalog_rejected(tmp_path, monkeypatch):
    for blank in ("", "   "):
        monkeypatch.setenv("FINANCE_SCOUT_EVAL_CONFIG", blank)
        with pytest.raises(ValueError, match="invalid_config_path"):
            load_scout_eval_config()
    monkeypatch.delenv("FINANCE_SCOUT_EVAL_CONFIG", raising=False)

    monkeypatch.setenv("FINANCE_SCOUT_EVAL_PROFILE", "missing_profile")
    with pytest.raises(ValueError, match="unknown_profile"):
        load_scout_eval_config()

    monkeypatch.delenv("FINANCE_SCOUT_EVAL_PROFILE", raising=False)

    bad_version = _write_toml(
        tmp_path,
        """
schema_version = 2
default_profile = "ling"
hard_spend_cap_usd = "0.00"

[profiles.ling]
model_id = "inclusionai/ling-3.2-flash:free"
input_usd_per_million_tokens = "0"
output_usd_per_million_tokens = "0"
""",
    )
    with pytest.raises((ValueError, ValidationError)):
        load_scout_eval_config(config_path=bad_version)

    secret_in_toml = _write_toml(
        tmp_path,
        """
schema_version = 1
default_profile = "ling"
hard_spend_cap_usd = "0.00"
openrouter_api_key = "sk-or-v1-leak"

[profiles.ling]
model_id = "inclusionai/ling-3.2-flash:free"
input_usd_per_million_tokens = "0"
output_usd_per_million_tokens = "0"
""",
    )
    with pytest.raises((ValueError, ValidationError)):
        ScoutEvalCatalog.from_toml_path(secret_in_toml)


@pytest.mark.parametrize(
    "cap,in_rate,out_rate",
    [
        ("-0.01", "0", "0"),
        ("NaN", "0", "0"),
        ("Infinity", "0", "0"),
        ("0.00", "-0.1", "0"),
        ("0.00", "0", "Inf"),
    ],
)
def test_nonfinite_or_negative_decimals_rejected(tmp_path, cap, in_rate, out_rate):
    path = _write_toml(
        tmp_path,
        f"""
schema_version = 1
default_profile = "paid"
hard_spend_cap_usd = "{cap}"

[profiles.paid]
model_id = "example/model-1"
input_usd_per_million_tokens = "{in_rate}"
output_usd_per_million_tokens = "{out_rate}"
""",
    )
    with pytest.raises((ValueError, ValidationError)):
        load_scout_eval_config(config_path=path, allow_paid_providers=True)


def test_positive_spend_requires_explicit_paid_authorization(tmp_path, monkeypatch):
    monkeypatch.delenv("FINANCE_ALLOW_PAID_PROVIDERS", raising=False)
    paid_cap = _write_toml(
        tmp_path,
        """
schema_version = 1
default_profile = "ling"
hard_spend_cap_usd = "0.05"

[profiles.ling]
model_id = "inclusionai/ling-3.2-flash:free"
input_usd_per_million_tokens = "0"
output_usd_per_million_tokens = "0"
""",
        name="paid-cap.toml",
    )
    with pytest.raises(ValueError, match="paid_providers_not_authorized"):
        load_scout_eval_config(config_path=paid_cap)

    paid_rates = _write_toml(
        tmp_path,
        """
schema_version = 1
default_profile = "paid"
hard_spend_cap_usd = "0.00"

[profiles.paid]
model_id = "example/paid-1"
input_usd_per_million_tokens = "0.15"
output_usd_per_million_tokens = "0.60"
""",
        name="paid-rates.toml",
    )
    with pytest.raises(ValueError, match="paid_providers_not_authorized"):
        load_scout_eval_config(config_path=paid_rates)

    monkeypatch.setenv("FINANCE_ALLOW_PAID_PROVIDERS", "maybe")
    with pytest.raises(ValueError):
        load_scout_eval_config(config_path=paid_cap)

    monkeypatch.setenv("FINANCE_ALLOW_PAID_PROVIDERS", "true")
    allowed = load_scout_eval_config(config_path=paid_cap)
    assert allowed.allow_paid_providers is True
    assert allowed.hard_spend_cap_usd == Decimal("0.05")

    explicit = load_scout_eval_config(config_path=paid_rates, allow_paid_providers=True)
    assert explicit.allow_paid_providers is True
    assert explicit.input_usd_per_million_tokens == Decimal("0.15")
