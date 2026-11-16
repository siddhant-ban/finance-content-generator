"""Versioned local telemetry wire contracts; no SDK objects are persisted."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import Field, field_validator, model_validator
from app.contracts.common import StrictModel
from app.observability.sanitize import sanitize_attributes, safe_event, safe_operation, safe_project

TraceID = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]
SpanID = Annotated[str, Field(pattern=r'^[0-9a-f]{16}$')]
Count = Annotated[int, Field(ge=0)]
Timestamp = Annotated[int, Field(ge=0, le=2**64-1)]
ErrorCode = Literal['capacity_exceeded', 'persistence_failed', 'export_failed', 'export_timeout', 'invalid_record', 'duplicate_conflict']

class SpanEvent(StrictModel):
    name: str
    timestamp_ns: Timestamp
    attributes: dict[str, object] = Field(default_factory=dict)
    _name = field_validator('name', mode='before')(safe_event)
    _attributes = field_validator('attributes', mode='before')(sanitize_attributes)

class SpanRecord(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    trace_id: TraceID
    span_id: SpanID
    parent_span_id: SpanID | None = None
    trace_flags: Literal[0, 1] = 1
    project: str
    name: str
    start_time_ns: Timestamp
    end_time_ns: Timestamp
    run_id: UUID | None = None
    story_id: UUID | None = None
    attributes: dict[str, object] = Field(default_factory=dict)
    events: tuple[SpanEvent, ...] = ()
    status: Literal['UNSET', 'OK', 'ERROR'] = 'UNSET'
    export_status: Literal['pending'] = 'pending'
    resource: dict[str, str] = Field(default_factory=dict)
    _name = field_validator('name', mode='before')(safe_operation)
    _project = field_validator('project', mode='before')(safe_project)
    _attributes = field_validator('attributes', mode='before')(sanitize_attributes)

    @model_validator(mode='after')
    def validate_record(self):
        if int(self.trace_id, 16) == 0 or int(self.span_id, 16) == 0 or self.parent_span_id == '0'*16:
            raise ValueError('nonzero W3C identifiers required')
        if self.end_time_ns < self.start_time_ns:
            raise ValueError('span end precedes start')
        object.__setattr__(self, 'resource', {'service.name': 'finance-content-generator', 'openinference.project.name': self.project})
        return self

class EnqueueResult(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    accepted: bool
    error_code: ErrorCode | None = None

class HealthReport(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    pending_count: Count | None
    pending_bytes: Count | None
    lost_records: Count = 0
    persistence_failures: Count = 0
    export_failures: Count = 0
    degraded_runs: tuple[UUID, ...] = ()
    error_code: ErrorCode | None = None
    degraded: bool

class ExportReport(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    exported: Count = 0
    pending_count: Count | None
    error_code: ErrorCode | None = None

class CollectorHealth(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    available: bool
    status_code: int | None = None
    error_code: Literal['unavailable', 'invalid_endpoint'] | None = None
