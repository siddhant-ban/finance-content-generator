"""Telemetry privacy boundary; unknown content never crosses it."""
import json
import pytest
from app.observability.sanitize import sanitize_attributes

SENTINELS = ('TOKEN_SENTINEL', 'COOKIE_SENTINEL', 'PASSWORD_SENTINEL', 'PrivateNameSentinel', 'ARTICLE_BODY_SENTINEL')

def test_nested_errors_urls_and_events_redacted():
    raw = {"token": SENTINELS[0], "cookie": SENTINELS[1], "password": SENTINELS[2],
           "reviewer": SENTINELS[3], "article.body": SENTINELS[4],
           "error.message": '/oauth/callback?code=' + SENTINELS[0],
           "model.parameters": {"temperature": 0.2, "top_p": 0.9, "password": SENTINELS[2], "nested": {"max_tokens": 99}},
           "source.ref": 'https://user:password@example.com/a?token=' + SENTINELS[0],
           "model.provider": "ollama", "retry.count": 2, "usage.input_tokens": None}
    safe = sanitize_attributes(raw)
    assert safe['model.parameters.temperature'] == 0.2
    assert safe['model.parameters.top_p'] == 0.9
    assert safe['model.provider'] == 'ollama'
    assert safe['retry.count'] == 2
    assert 'usage.input_tokens' not in safe
    assert not any(value in json.dumps(safe) for value in SENTINELS)
    assert not any(key in safe for key in ('error.message', 'source.ref', 'model.parameters.nested'))

def test_unknown_numeric_nested_and_nonfinite_values_dropped():
    assert sanitize_attributes({'model.parameters': {'temperature': float('nan'), 'nested': {'temperature': 0.4}}, 'retry.count': True, 'unknown': 4}) == {}

def test_required_metadata_uses_actual_project_identifiers():
    from pathlib import Path
    from app.prompts import load_prompt
    prompt = load_prompt(Path('prompts/manifests/foundation.v1.json'))
    expected = {
        'agent.name': 'researcher', 'agent.version': '0.1.0',
        'prompt.version': prompt.prompt_version, 'config.version': '1.0',
        'model.provider': 'api', 'model.name': 'benchmarked-model:release1',
        'dataset.name': 'foundation', 'dataset.version': '1.0.0',
        'environment': 'local', 'source.count': 2, 'claim.count': 3,
        'latency_ms': 1.25, 'retry.count': 0, 'error.type': 'timeout',
        'input.ref': 'a' * 64, 'output.ref': 'b' * 64, 'usage.currency': 'CAD',
    }
    assert sanitize_attributes(expected) == expected

def test_oversized_integers_do_not_break_the_privacy_boundary():
    assert sanitize_attributes({'model.parameters': {'seed': 10**1000},
                                'source.count': 10**1000}) == {}

@pytest.mark.parametrize('name', ['llama3', 'o3', 'org/Qwen2.5-0.5B-Instruct'])
def test_versioned_model_identifiers_do_not_require_a_selected_runtime(name):
    assert sanitize_attributes({'model.name': name}) == {'model.name': name}
