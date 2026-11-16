"""Environment-only configuration with safe defaults and secret-free hashes."""
import hashlib
import json
import os
from pathlib import Path
from ipaddress import ip_address
from urllib.parse import urlsplit
from typing import Literal
from pydantic import Field, SecretStr, model_validator
from app.contracts.common import StrictModel

def validate_collector_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    try:
        local = ip_address(parsed.hostname or '').is_loopback
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        local = valid_port = False
    if (parsed.scheme != 'http' or not local or not valid_port or
            parsed.path != '/v1/traces' or parsed.username is not None or
            parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('collector must be an HTTP loopback /v1/traces endpoint')
    return value

class Settings(StrictModel):
    config_version: Literal["1.2"] = "1.2"
    environment: Literal["local", "test", "demo"] = "local"
    publishing_enabled: Literal[False] = False
    allow_paid_providers: bool = False
    model_provider: Literal["ollama", "api"] | None = None
    fake_stage_enabled: bool = False
    data_dir: Path = Path("data")
    phoenix_endpoint: str = 'http://127.0.0.1:6006/v1/traces'
    github_client_secret: SecretStr | None = Field(default=None, repr=False, exclude=True)

    github_client_id: str | None = Field(default=None, repr=False, exclude=True)
    github_allowed_ids: tuple[int, ...] = Field(default=(), repr=False, exclude=True)
    auth_callback_url: str = Field(default="http://127.0.0.1:8765/auth/github/callback", exclude=True)
    auth_loopback_development: bool = Field(default=False, exclude=True)

    @property
    def auth_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret
                    and self.github_client_secret.get_secret_value() and self.github_allowed_ids)

    @model_validator(mode="after")
    def auth_policy(self):
        if self.auth_callback_url != "http://127.0.0.1:8765/auth/github/callback":
            raise ValueError("exact loopback auth callback required")
        if any(type(value) is not int or value <= 0 for value in self.github_allowed_ids):
            raise ValueError("positive numeric GitHub IDs required")
        return self

    @model_validator(mode="after")
    def policy_defaults(self):
        validate_collector_endpoint(self.phoenix_endpoint)
        if self.fake_stage_enabled and self.environment not in {"demo", "test"}:
            raise ValueError("fake stages require demo or test environment")
        if self.model_provider == "api" and not self.allow_paid_providers:
            raise ValueError("API providers require explicit paid-provider opt-in")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        values = {}
        boolean_fields = {"publishing_enabled", "allow_paid_providers", "fake_stage_enabled", "auth_loopback_development"}
        for name in cls.model_fields:
            raw = os.environ.get("FINANCE_" + name.upper())
            if raw is None:
                continue
            if name in boolean_fields:
                if raw.lower() not in {"true", "false"}:
                    raise ValueError("configuration boolean must be true or false")
                values[name] = raw.lower() == "true"
            elif name == "github_allowed_ids":
                values[name] = tuple(int(value.strip()) for value in raw.split(",") if value.strip())
            elif name == "data_dir":
                values[name] = Path(raw)
            elif name == "github_client_secret":
                values[name] = SecretStr(raw)
            else:
                values[name] = raw
        return cls(**values)

    def config_hash(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"),sort_keys=True,separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()
