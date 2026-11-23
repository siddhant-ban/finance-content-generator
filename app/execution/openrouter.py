"""Single-request synthetic demo adapter for the free OpenRouter route."""
from decimal import Decimal
import hashlib
import os
from pathlib import Path

import httpx

from app.contracts.common import Money, Usage
from app.contracts.execution import SyntheticData
from app.execution.fakes import FakeStage
from app.execution.protocols import (
    ModelConfiguration, ModelRequest, ModelResponse, PolicyError, ProviderResponseError, RunContext,
)
from app.observability.sanitize import sanitize_attributes
from app.prompts import PromptSpec


OPENROUTER_ROUTE = 'openrouter/free'
OPENROUTER_PROMPT = Path('prompts/manifests/foundation_openrouter.v1.json')
# The direct adapter can send only this reviewed prompt, even outside AgentRunner.
_PROMPT_PATH = '../foundation/openrouter_stage.v1.md'
_PROMPT_SHA256 = '5d6bb5451d69a739dc4c8e51010ff574fc0da62d25edbca512e6dccef5405ce1'
_ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'


def openrouter_configuration() -> ModelConfiguration:
    return ModelConfiguration(provider='openrouter', model=OPENROUTER_ROUTE,
                              parameters={'max_tokens': 1024})


class OpenRouterResponse(ModelResponse[SyntheticData]):
    resolved_model: str | None = None


def _usage(payload: object) -> Usage:
    if not isinstance(payload, dict):
        return Usage()
    def tokens(field: str) -> int | None:
        value = payload.get(field)
        return value if type(value) is int and 0 <= value < 2**63 else None
    cost = payload.get('cost')
    amount = Decimal(str(cost)) if type(cost) in (int, float) else None
    return Usage(input_tokens=tokens('prompt_tokens'), output_tokens=tokens('completion_tokens'),
                 measured_cost=Money(amount=amount, currency='USD')
                 if amount is not None and amount.is_finite() and 0 <= amount <= 1e9 else None)


class OpenRouterProvider:
    def __init__(self, prompt: PromptSpec) -> None:
        if (prompt.path != _PROMPT_PATH or prompt.sha256 != _PROMPT_SHA256
                or prompt.prompt_id != 'foundation_openrouter'
                or prompt.prompt_version != 'foundation_openrouter.v1'):
            raise ValueError('openrouter_prompt_mismatch')
        self._api_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
        if not self._api_key:
            raise ValueError('openrouter_credential_missing')
        self.prompt = prompt
        self._prompt_path = (OPENROUTER_PROMPT.parent / _PROMPT_PATH).resolve()

    def generate(self, request: ModelRequest) -> OpenRouterResponse:
        configuration = openrouter_configuration()
        if (any(getattr(request, field) != getattr(configuration, field)
                for field in ModelConfiguration.model_fields)
                or request.content != SyntheticData(value=4)):
            raise PolicyError('openrouter_synthetic_request_denied')
        prompt_bytes = self._prompt_path.read_bytes()
        if hashlib.sha256(prompt_bytes).hexdigest() != _PROMPT_SHA256:
            raise ValueError('openrouter_prompt_mismatch')

        try:
            with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                response = client.post(_ENDPOINT, headers={'Authorization': f'Bearer {self._api_key}'},
                    json={'model': OPENROUTER_ROUTE, **configuration.parameters, 'messages': [
                        {'role': 'system', 'content': prompt_bytes.decode('utf-8')},
                        {'role': 'user', 'content': request.content.canonical_bytes().decode('utf-8')},
                    ]})
                response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL):
            # The runner retries its transient exceptions. This demo is deliberately single-shot.
            raise RuntimeError('openrouter_request_failed') from None

        try:
            payload = response.json()
        except ValueError:
            raise ProviderResponseError(Usage(), None) from None
        if not isinstance(payload, dict):
            raise ProviderResponseError(Usage(), None)
        usage = _usage(payload.get('usage'))
        safe_model = sanitize_attributes(
            {'model.resolved_name': payload.get('model')}).get('model.resolved_name')
        if safe_model is not None and self._api_key in safe_model:
            safe_model = None

        try:
            if 'error' in payload:
                raise ValueError('provider indicated an error')
            choice, = payload['choices']
            if choice['finish_reason'] != 'stop':
                raise ValueError('completion did not finish normally')
            content = SyntheticData.model_validate_json(choice['message']['content'])
            if content.value != request.content.value + 1:
                raise ValueError('unexpected result')
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderResponseError(usage, safe_model) from None
        return OpenRouterResponse(**configuration.model_dump(), content=content, usage=usage,
                                  resolved_model=safe_model)


class OpenRouterStage(FakeStage):
    prompt_id = 'foundation_openrouter'
    prompt_version = 'foundation_openrouter.v1'

    def __init__(self, prompt: PromptSpec) -> None:
        super().__init__(OpenRouterProvider(prompt))

    def run(self, value: SyntheticData, context: RunContext) -> SyntheticData:
        request = ModelRequest(**openrouter_configuration().model_dump(), content=value)
        return context.generate(self.provider, request).content

    def validate_output(self, value: SyntheticData) -> None:
        if value != SyntheticData(value=5):
            raise ValueError('openrouter_invalid_result')
