import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
import pytest
from pydantic import ValidationError
from app.contracts.common import StrictModel, Money, Percent, PercentagePoints, ArtifactRef, Usage
from app.contracts.execution import InputEnvelope, OutputEnvelope, ExecutionError, decode_envelope

class Payload(StrictModel):
    value: int

@pytest.fixture
def envelope_fields():
    return dict(run_id=UUID(int=1), story_id=UUID(int=2), invocation_id=UUID(int=3),
                agent_name="foundation", agent_version="0.1.0", prompt_id="foundation",
                prompt_version="foundation.v1", prompt_hash="a"*64, config_hash="b"*64, config_version="1.0",
                created_at=datetime(2026,11,10,tzinfo=timezone.utc))

@pytest.fixture
def ref():
    return ArtifactRef(sha256="c"*64, media_type="application/json", size_bytes=12)

def test_unknown_major_and_extra_fields_rejected(envelope_fields,ref):
    data=InputEnvelope[Payload](**envelope_fields,input=Payload(value=1),input_ref=ref).model_dump(mode="json")
    for changes in ({"schema_version":"2.0"}, {"surprise":True}, {"input":{"value":"1"}}):
        with pytest.raises(ValidationError):
            decode_envelope(json.dumps(data|changes).encode(),Payload)

def test_naive_date_rejected_offset_normalized(envelope_fields,ref):
    with pytest.raises(ValidationError):
        InputEnvelope[Payload](**(envelope_fields|{"created_at":datetime(2026,11,10)}),input=Payload(value=1),input_ref=ref)
    local=datetime(2026,11,10,8,tzinfo=timezone(timedelta(hours=8)))
    item=InputEnvelope[Payload](**(envelope_fields|{"created_at":local}),input=Payload(value=1),input_ref=ref)
    assert item.model_dump(mode="json")["created_at"] == "2026-11-10T00:00:00Z"
    decoded=decode_envelope(item.model_dump_json().encode(),Payload)
    assert decoded == item

def test_percent_not_percentage_points():
    rate=Percent(value=Decimal("0.5"))
    with pytest.raises(ValidationError):
        PercentagePoints.model_validate(rate)
    assert rate.model_dump(mode="json") == {"unit":"percent","value":"0.5"}

def test_money_rejects_float_and_invalid_currency():
    with pytest.raises(ValidationError):
        Money(amount=0.1,currency="USD")
    with pytest.raises(ValidationError):
        Money(amount=Decimal("1"),currency="usd")
    assert Money(amount=Decimal("1.25"),currency="USD").model_dump(mode="json")["amount"] == "1.25"

def test_failed_envelope_requires_error(envelope_fields,ref):
    with pytest.raises(ValidationError):
        OutputEnvelope[Payload](**envelope_fields,status="FAILED",output=Payload(value=1),output_ref=ref)
    with pytest.raises(ValidationError):
        OutputEnvelope[Payload](**envelope_fields,status="OK",error=ExecutionError(code="bad_input",retryable=False))
    blocked=OutputEnvelope[Payload](**envelope_fields,status="BLOCKED",error=ExecutionError(code="evidence_gap",retryable=False))
    assert blocked.output is None
    assert blocked.output_ref is None

def test_unknown_usage_is_null_not_zero():
    usage=Usage()
    assert usage.input_tokens is None
    assert usage.measured_cost is None
    assert usage.estimated_cost is None
    assert usage.availability_reason == "not_reported"

def test_negative_usage_rejected():
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1)

def test_schema_snapshots_export_and_decode(tmp_path,envelope_fields,ref):
    from app.contracts.export import export_schemas
    from pathlib import Path
    exported=export_schemas(tmp_path)
    assert {"common.json","execution.json"}.issubset(exported)
    for name in exported:
        assert json.loads((tmp_path/name).read_text()) == json.loads((Path("schemas/v1")/name).read_text())
    data=InputEnvelope[Payload](**envelope_fields,input=Payload(value=2),input_ref=ref).model_dump_json().encode()
    assert decode_envelope(data,Payload).input.value == 2


@pytest.mark.parametrize("model,data", [
    (Money,{"amount":Decimal("0.10"),"currency":"USD"}),
    (Percent,{"value":Decimal("0.5")}),
    (PercentagePoints,{"value":Decimal("0.5")}),
])
def test_exact_decimal_json_roundtrip(model,data):
    value=model(**data)
    assert model.model_validate_json(value.model_dump_json()) == value

@pytest.mark.parametrize("bad", [0.1, True, "NaN", "Infinity", "invalid"])
def test_json_decimal_numeric_nonfinite_and_invalid_rejected(bad):
    with pytest.raises(ValidationError):
        Money.model_validate_json(json.dumps({"amount":bad,"currency":"USD"}))

def test_config_version_required(envelope_fields,ref):
    fields=envelope_fields.copy()
    fields.pop("config_version")
    with pytest.raises(ValidationError):
        InputEnvelope[Payload](**fields,input=Payload(value=1),input_ref=ref)
    valid=InputEnvelope[Payload](**envelope_fields,input=Payload(value=1),input_ref=ref)
    assert valid.config_version == "1.0"


@pytest.mark.parametrize("model,field", [(Money,"amount"),(Percent,"value"),(PercentagePoints,"value")])
def test_decimal_schema_advertises_strings_only(model,field):
    schema=model.model_json_schema()["properties"][field]
    assert schema["type"] == "string"
    assert "anyOf" not in schema
