"""Typed local execution boundaries. No publication or approval capabilities."""
from contextlib import contextmanager
from typing import Generic, Protocol, TypeVar
from typing_extensions import TypeVar as DefaultTypeVar
import math
from pydantic import Field, field_validator
from app.contracts.common import StrictModel, Nonempty, Usage, Money
from app.contracts.execution import SyntheticData

I = TypeVar('I', bound=StrictModel)
O = TypeVar('O', bound=StrictModel)
Payload = DefaultTypeVar('Payload', bound=StrictModel, default=SyntheticData)

class PolicyError(ValueError):
    """A deterministic policy boundary denied execution."""

class TransientProviderError(Exception):
    """Explicitly retryable provider/network failure; messages remain private."""

class ModelConfiguration(StrictModel):
    provider: Nonempty = 'fake'
    model: Nonempty = 'fake'
    parameters: dict[str, int | float] = Field(default_factory=dict)

    @field_validator('parameters')
    @classmethod
    def safe_parameters(cls, values):
        allowed = {'temperature','top_p','top_k','max_tokens','seed','frequency_penalty','presence_penalty'}
        if set(values) - allowed or any(type(v) not in (int,float) or not math.isfinite(v) for v in values.values()):
            raise ValueError('only finite, known model parameters allowed')
        return values

class ModelRequest(ModelConfiguration, Generic[Payload]):
    content: Payload

class ModelResponse(ModelConfiguration, Generic[Payload]):
    content: Payload
    usage: Usage = Field(default_factory=Usage)

class Provider(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...

class RunContext:
    """Narrow instrumented provider/tool facade, with no workflow authority."""
    __slots__ = ('__telemetry', '__configuration', '__usages')

    def __init__(self, telemetry, configuration: ModelConfiguration):
        self.__telemetry = telemetry
        self.__configuration = ModelConfiguration.model_validate_json(configuration.model_dump_json())
        self.__usages = []

    @property
    def usage(self) -> Usage:
        if not self.__usages:
            return Usage()
        def tokens(name):
            values = [getattr(u,name) for u in self.__usages]
            return None if any(v is None for v in values) else sum(values)
        def cost(name):
            values = [getattr(u,name) for u in self.__usages]
            if any(v is None for v in values) or len({v.currency for v in values}) != 1:
                return None
            return Money(amount=sum(v.amount for v in values),currency=values[0].currency)
        input_tokens, output_tokens = tokens('input_tokens'),tokens('output_tokens')
        measured, estimated = cost('measured_cost'),cost('estimated_cost')
        return Usage(input_tokens=input_tokens,output_tokens=output_tokens,
            measured_cost=measured,estimated_cost=estimated,
            availability_reason='not_reported' if None in (input_tokens,output_tokens,measured) else None)

    @contextmanager
    def tool_span(self):
        with self.__telemetry.span('tool.call', 'finance-media-orchestrator', {}) as span:
            yield span

    def generate(self, provider: Provider, request: ModelRequest) -> ModelResponse:
        request = type(request).model_validate_json(request.model_dump_json())
        config = {k: request.model_dump()[k] for k in ModelConfiguration.model_fields}
        if config != self.__configuration.model_dump() or request.provider != 'fake':
            raise PolicyError('provider configuration denied')
        # Task6 deliberately admits only the local fake implementation.
        from app.execution.fakes import FakeProvider
        if type(provider) is not FakeProvider:
            raise PolicyError('only the synthetic provider is available')
        with self.__telemetry.span('model.call', 'finance-media-orchestrator', {
            'model.provider': request.provider, 'model.name': request.model,
            'model.parameters': request.parameters}) as span:
            self.__usages.append(Usage())
            response = provider.generate(request)
            response = type(response).model_validate_json(response.model_dump_json())
            if {k: response.model_dump()[k] for k in ModelConfiguration.model_fields} != config:
                raise ValueError('response metadata mismatch')
            self.__usages[-1] = response.usage
            span.set_attributes(usage_attributes(response.usage))
            return response

def usage_attributes(usage: Usage) -> dict:
    result = {'usage.availability_reason': usage.availability_reason}
    if usage.input_tokens is not None: result['usage.input_tokens'] = usage.input_tokens
    if usage.output_tokens is not None: result['usage.output_tokens'] = usage.output_tokens
    for name in ('measured_cost', 'estimated_cost'):
        cost = getattr(usage, name)
        if cost is not None:
            result['usage.' + name] = float(cost.amount)
            result['usage.currency'] = cost.currency
    return result

class Stage(Protocol, Generic[I, O]):
    agent_name: str
    agent_version: str
    prompt_id: str
    prompt_version: str
    output_type: type[O]
    def validate_input(self, value: I) -> None: ...
    def run(self, value: I, context: RunContext) -> O: ...
    def validate_output(self, value: O) -> None: ...
