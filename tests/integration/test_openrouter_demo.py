from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from app.contracts.common import ArtifactRef
from app.contracts.execution import AttemptRecord, OutputEnvelope, SyntheticData
from app.demo import main
from app.observability.models import SpanRecord
from app.storage.artifacts import ArtifactStore


TEST_KEY = 'sk-or-v1-synthetic-test-credential'


@pytest.fixture
def mock_openrouter(monkeypatch):
    real_client = httpx.Client
    requests = []

    def install(handler, *, credential=True):
        if credential:
            monkeypatch.setenv('OPENROUTER_API_KEY', TEST_KEY)
        else:
            monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)

        def respond(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            return real_client(**kwargs, transport=httpx.MockTransport(respond))

        monkeypatch.setattr(httpx, 'Client', client)
        return requests

    return install


def completion(content, *, model='example/model-1:free'):
    return httpx.Response(200, json={
        'model': model,
        'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 43, 'completion_tokens': 7},
    })


def read_journal(output_dir):
    with closing(sqlite3.connect(output_dir / 'state.db')) as conn:
        stories = conn.execute('SELECT state, approval_id, payload FROM stories').fetchall()
        attempts = [AttemptRecord.model_validate_json(raw) for raw, in
                    conn.execute('SELECT payload FROM execution_attempts')]
    return stories, attempts


def test_openrouter_candidate_uses_the_one_live_result(tmp_path, mock_openrouter):
    requests = mock_openrouter(lambda request: completion('{"value":5}'))

    assert main(['--offline', '--openrouter', '--output', str(tmp_path)]) == 0

    request, = requests
    assert request.method == 'POST'
    assert str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
    assert request.headers['Authorization'] == f'Bearer {TEST_KEY}'
    body = json.loads(request.content)
    assert body['model'] == 'openrouter/free'
    assert body['max_tokens'] == 1024
    system, user = body['messages']
    assert system['role'] == 'system'
    assert user == {'role': 'user', 'content': '{"value":4}'}

    stories, attempts = read_journal(tmp_path)
    (state, approval_id, raw_story), = stories
    assert state == 'AWAITING_APPROVAL'
    assert approval_id is None
    live, = [attempt for attempt in attempts if attempt.provider == 'openrouter']
    assert (live.model, live.retry_count, live.status) == ('openrouter/free', 0, 'COMPLETED')
    output = OutputEnvelope[SyntheticData].model_validate_json(live.output_envelope)
    assert output.output == SyntheticData(value=5)
    assert (output.usage.input_tokens, output.usage.output_tokens) == (43, 7)
    report = json.loads((tmp_path / 'demo-report.json').read_bytes())
    assert report['status'] == 'PASS'
    assert report['prompt_hash'] == live.prompt_hash
    assert json.loads((tmp_path / report['runner_output_path']).read_bytes()) == {'value': 5}

    candidate_ref = ArtifactRef.model_validate_json(json.dumps(json.loads(raw_story)['artifacts']['candidate']))
    with closing(sqlite3.connect(tmp_path / 'state.db')) as conn:
        candidate = json.loads(ArtifactStore(tmp_path / 'artifacts', conn).read(candidate_ref))
    assert candidate['runner_output']['sha256'] == output.output_ref.sha256 == report['runner_output_hash']

    spans = [SpanRecord.model_validate_json(line) for line in
             (tmp_path / 'traces' / 'spans.jsonl').read_bytes().splitlines()]
    model, = [span for span in spans if span.name == 'model.call' and span.trace_id == live.trace_id]
    assert model.parent_span_id == live.span_id
    assert model.attributes['model.provider'] == 'openrouter'
    assert model.attributes['model.name'] == 'openrouter/free'
    assert model.attributes['model.resolved_name'] == 'example/model-1:free'
    assert model.attributes['usage.input_tokens'] == 43
    assert model.attributes['usage.output_tokens'] == 7
    assert report['telemetry']['local_evidence']['complete']
    assert set(report['experiments']) == {'dev', 'holdout', 'prod_regression'}
    for experiment in report['experiments'].values():
        assert experiment['status'] == 'PASS'
        assert experiment['offline_verified']
        assert experiment['provenance']['provider'] == 'fake'
        assert experiment['provenance']['model'] == 'fake'


def test_configured_key_does_not_enable_openrouter(tmp_path, mock_openrouter):
    requests = mock_openrouter(lambda request: completion('{"value":5}'))

    assert main(['--offline', '--output', str(tmp_path)]) == 0

    assert requests == []
    assert {attempt.provider for attempt in read_journal(tmp_path)[1]} == {'fake'}


def test_missing_key_stops_before_http(tmp_path, mock_openrouter):
    requests = mock_openrouter(lambda request: completion('{"value":5}'), credential=False)

    assert main(['--offline', '--openrouter', '--output', str(tmp_path)]) == 2

    assert requests == []
    assert not (tmp_path / 'demo-report.json').exists()


@pytest.mark.parametrize('forge_field', ['path', 'sha256'])
def test_direct_provider_rejects_a_forged_prompt_pin(tmp_path, mock_openrouter, forge_field):
    from app.execution.openrouter import OPENROUTER_PROMPT, OpenRouterProvider
    from app.prompts import load_prompt

    requests = mock_openrouter(lambda request: completion('{"value":5}'))
    alternative = tmp_path / 'synthetic-alternative.md'
    alternative.write_bytes(b'different synthetic instructions')
    prompt = load_prompt(OPENROUTER_PROMPT)
    forged = prompt.model_copy(update={
        forge_field: (str(alternative) if forge_field == 'path'
                      else hashlib.sha256(alternative.read_bytes()).hexdigest()),
    })

    with pytest.raises(ValueError, match='openrouter_prompt_mismatch'):
        OpenRouterProvider(forged)

    assert requests == []


def test_prompt_mutation_after_runner_pin_stops_before_http(tmp_path, mock_openrouter, monkeypatch):
    from app.execution import openrouter

    requests = mock_openrouter(lambda request: completion('{"value":5}'))
    prompt_root = tmp_path / 'prompt-copy'
    manifest_path = prompt_root / openrouter.OPENROUTER_PROMPT
    manifest_path.parent.mkdir(parents=True)
    manifest = json.loads(openrouter.OPENROUTER_PROMPT.read_bytes())
    prompt_path = (manifest_path.parent / manifest['path']).resolve()
    prompt_path.parent.mkdir()
    original = Path(openrouter.OPENROUTER_PROMPT.parent / manifest['path']).read_bytes()
    prompt_path.write_bytes(original)
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(openrouter, 'OPENROUTER_PROMPT', manifest_path)
    generate = openrouter.OpenRouterProvider.generate

    def change_prompt_then_generate(provider, request):
        modified = original + b' changed after the runner pinned the prompt'
        prompt_path.write_bytes(modified)
        manifest['sha256'] = hashlib.sha256(modified).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        return generate(provider, request)

    monkeypatch.setattr(openrouter.OpenRouterProvider, 'generate', change_prompt_then_generate)
    output_dir = tmp_path / 'demo'
    assert main(['--offline', '--openrouter', '--output', str(output_dir)]) == 2
    assert requests == []
    assert read_journal(output_dir)[0] == []


@pytest.mark.parametrize('failure', ['api', 'timeout', 'malformed', 'wrong_value', 'coerced_value'])
def test_failed_openrouter_call_creates_no_candidate_or_retry(tmp_path, mock_openrouter, capsys, failure):
    def respond(request):
        if failure == 'api':
            return httpx.Response(429, text=TEST_KEY)
        if failure == 'timeout':
            raise httpx.ReadTimeout(TEST_KEY, request=request)
        return completion({
            'malformed': TEST_KEY,
            'wrong_value': '{"value":6}',
            'coerced_value': '{"value":"5"}',
        }[failure])

    requests = mock_openrouter(respond)

    assert main(['--offline', '--openrouter', '--output', str(tmp_path)]) == 2

    assert len(requests) == 1
    stories, attempts = read_journal(tmp_path)
    assert stories == []
    attempt, = attempts
    assert (attempt.provider, attempt.retry_count, attempt.status) == ('openrouter', 0, 'FAILED')
    spans = [SpanRecord.model_validate_json(line) for line in
             (tmp_path / 'traces' / 'spans.jsonl').read_bytes().splitlines()]
    model, = [span for span in spans if span.name == 'model.call' and span.trace_id == attempt.trace_id]
    assert model.parent_span_id == attempt.span_id
    if failure in {'malformed', 'wrong_value', 'coerced_value'}:
        assert model.attributes['model.resolved_name'] == 'example/model-1:free'
        assert model.attributes['usage.input_tokens'] == 43
        assert model.attributes['usage.output_tokens'] == 7
        output = OutputEnvelope[SyntheticData].model_validate_json(attempt.output_envelope)
        assert (output.usage.input_tokens, output.usage.output_tokens) == (43, 7)
    assert TEST_KEY not in capsys.readouterr().out
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert TEST_KEY.encode() not in path.read_bytes()
