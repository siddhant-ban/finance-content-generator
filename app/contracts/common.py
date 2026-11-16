"""Shared validated values; no provider-specific payloads or side effects."""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationInfo, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, validate_default=True)

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def record_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)

UtcDatetime = Annotated[datetime, AfterValidator(_utc)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Nonempty = Annotated[str, Field(min_length=1)]

def _decimal(value: object, info: ValidationInfo) -> object:
    if info.mode == "json":
        if not isinstance(value, str):
            raise ValueError("decimal values must be JSON strings")
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ValueError("invalid decimal string") from error
    return value

ExactDecimal = Annotated[Decimal, BeforeValidator(_decimal, json_schema_input_type=str), Field(allow_inf_nan=False)]

class Money(StrictModel):
    amount: ExactDecimal
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

class Percent(StrictModel):
    unit: Literal["percent"] = "percent"
    value: ExactDecimal

class PercentagePoints(StrictModel):
    unit: Literal["percentage_points"] = "percentage_points"
    value: ExactDecimal

class ArtifactRef(StrictModel):
    sha256: Sha256
    media_type: Nonempty
    size_bytes: Annotated[int, Field(ge=0)]
    schema_version: Literal["1.0"] = "1.0"

class Usage(StrictModel):
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    measured_cost: Money | None = None
    estimated_cost: Money | None = None
    availability_reason: Nonempty | None = "not_reported"

    @model_validator(mode="after")
    def validate_measurements(self):
        for cost in (self.measured_cost, self.estimated_cost):
            if cost is not None and cost.amount < 0:
                raise ValueError("usage cost cannot be negative")
        if any(value is None for value in (self.input_tokens,self.output_tokens,self.measured_cost)) and not self.availability_reason:
            raise ValueError("missing measurements require an availability reason")
        return self

class Principal(StrictModel):
    provider: Nonempty
    account_id: Annotated[int, Field(gt=0)]
    kind: Literal["human", "agent"]
    session_id: Nonempty | None = None

    @model_validator(mode="after")
    def human_requires_session(self):
        if self.kind == "human" and self.session_id is None:
            raise ValueError("human principal requires a server-side session reference")
        return self
