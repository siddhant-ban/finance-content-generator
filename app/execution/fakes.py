"""Synthetic arithmetic fixtures; no news, remote calls, or publication."""
from app.contracts.execution import SyntheticData
from app.execution.protocols import ModelRequest, ModelResponse, RunContext

class FakeProvider:
    def __init__(self, *, responses=(), errors=()):
        self.responses = list(responses)
        self.errors = list(errors)

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self.errors:
            raise self.errors.pop(0)
        if self.responses:
            return self.responses.pop(0)
        value = SyntheticData.model_validate(request.content.model_dump())
        return ModelResponse(provider=request.provider, model=request.model,
            parameters=dict(request.parameters), content=SyntheticData(value=value.value + 1))

class FakeStage:
    agent_name = 'foundation'
    agent_version = '1.0'
    prompt_id = 'foundation'
    prompt_version = 'foundation.v1'
    output_type = SyntheticData

    def __init__(self, provider=None):
        self.provider = provider if provider is not None else FakeProvider()

    def validate_input(self, value: SyntheticData) -> None:
        SyntheticData.model_validate(value.model_dump())

    def run(self, value: SyntheticData, context: RunContext) -> SyntheticData:
        response = context.generate(self.provider, ModelRequest(content=value))
        return SyntheticData.model_validate(response.content.model_dump())

    def validate_output(self, value: SyntheticData) -> None:
        SyntheticData.model_validate(value.model_dump())
