"""Deny-by-default telemetry vocabulary. Never stringify unknown objects."""
import math
import re
from collections.abc import Mapping
from uuid import UUID

PROJECTS = frozenset('finance-media-' + name for name in (
    'news-scout', 'novelty', 'research', 'fact-check', 'scriptwriter', 'producer', 'growth', 'orchestrator'))
OPERATIONS = frozenset(('pipeline.run', 'agent.invoke', 'model.call', 'tool.call',
    'evaluator.run', 'workflow.transition', 'approval.decision', 'render.run', 'publish.run', 'telemetry.operation'))
EVENTS = frozenset(('exception', 'human_decision', 'evaluator.error', 'state.transition', 'source.used', 'tool.completed', 'telemetry.event'))
ERRORS = frozenset(('exception', 'timeout', 'unavailable', 'invalid_input', 'policy_denied',
    'capacity_exceeded', 'persistence_failed', 'export_failed', 'export_timeout', 'invalid_record', 'duplicate_conflict'))
PARAMETERS = frozenset(('temperature', 'top_p', 'top_k', 'max_tokens', 'seed', 'frequency_penalty', 'presence_penalty'))
NUMBERS = frozenset(('retry.count', 'latency.ms', 'latency_ms', 'source.count', 'claim.count', 'usage.input_tokens', 'usage.output_tokens',
    'usage.measured_cost', 'usage.estimated_cost'))
ENUMS = {
    'evaluator.id': frozenset(('contract','policy','evidence','state','metadata','fake_rubric')),
    'model.provider': frozenset(('ollama', 'api', 'openai', 'anthropic', 'fake', 'local')),
    'agent.name': frozenset(('news_scout', 'novelty_analyst', 'researcher', 'fact_checker', 'growth_analyst',
                             'news-scout', 'novelty', 'research', 'fact-check', 'scriptwriter', 'producer', 'growth', 'orchestrator', 'foundation')),
    'result.status': frozenset(('OK', 'FAILED', 'BLOCKED', 'NEEDS_RESEARCH', 'REJECTED', 'AWAITING_APPROVAL')),
    'pipeline.stage': frozenset(('candidate', 'cluster', 'score', 'dossier', 'verification', 'script', 'render', 'approval', 'publish')),
    'error.code': ERRORS,
    'error.type': ERRORS,
    'environment': frozenset(('local', 'test', 'demo', 'production')),
    'usage.availability_reason': frozenset(('not_reported', 'unavailable')),
}

def _number(value: object) -> bool:
    # OTLP integer attributes are signed int64. Avoid float conversion overflow.
    return ((type(value) is int and -(2**63) <= value < 2**63) or
            (type(value) is float and math.isfinite(value)))

def _model_identifier(value: str) -> bool:
    # Versioned identifiers are metadata, not a list of selected runtimes. Free
    # text, filesystem paths, URLs and common credential prefixes are excluded.
    return (value == 'fake' or
            (len(value) <= 128 and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]*', value))
             and bool(re.search(r'[0-9]', value))
             and '://' not in value and '..' not in value
             and not re.match(r'[A-Za-z]:/', value)
             and not value.lower().startswith(('sk-', 'ghp_', 'gho_', 'github_pat_', 'token', 'password'))))

def safe_operation(value: object) -> str:
    return value if isinstance(value, str) and value in OPERATIONS else 'telemetry.operation'

def safe_event(value: object) -> str:
    return value if isinstance(value, str) and value in EVENTS else 'telemetry.event'

def safe_project(value: object) -> str:
    return value if isinstance(value, str) and value in PROJECTS else 'finance-media-orchestrator'

def sanitize_attributes(attributes: Mapping[str, object]) -> dict[str, object]:
    """Retain fixed enums, UUID/hash references and known finite numeric fields only.

    URLs, identity, free text, exception strings and unknown nested objects are
    dropped, including strings under otherwise plausible metadata keys.
    """
    safe = {}
    for key, value in attributes.items():
        if not isinstance(key, str):
            continue
        if key == 'model.parameters' and isinstance(value, Mapping):
            for parameter in PARAMETERS:
                number = value.get(parameter)
                if _number(number):
                    safe['model.parameters.' + parameter] = number
        elif key.startswith('model.parameters.') and key.removeprefix('model.parameters.') in PARAMETERS:
            if _number(value):
                safe[key] = value
        elif key in NUMBERS:
            if _number(value) and value >= 0:
                if key in ('retry.count', 'source.count', 'claim.count', 'usage.input_tokens', 'usage.output_tokens') and type(value) is not int:
                    continue
                safe[key] = value
        elif key in ENUMS:
            if isinstance(value, str) and value in ENUMS[key]:
                safe[key] = value
        elif key in ('run.id', 'story.id', 'invocation.id', 'claim.id', 'decision_id', 'source.id', 'tool.id', 'experiment.id'):
            if isinstance(value, UUID):
                safe[key] = str(value)
            elif isinstance(value, str):
                try:
                    safe[key] = str(UUID(value))
                except ValueError:
                    pass
        elif key in ('source.ref', 'tool.ref', 'input.ref', 'output.ref', 'prompt.hash', 'config.hash', 'artifact.hash'):
            if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
                safe[key] = value
        elif key in ('agent.version', 'prompt.version', 'dataset.version', 'config.version'):
            if isinstance(value, str) and re.fullmatch(r'(?:[a-z][a-z0-9_]*\.)?v?[0-9]+(?:\.[0-9]+){0,3}', value):
                safe[key] = value
        elif key == 'dataset.name':
            names = ENUMS['agent.name'] | {'golden', 'prod_regression'}
            if isinstance(value, str) and value in names:
                safe[key] = value
        elif key == 'model.name':
            if isinstance(value, str) and _model_identifier(value):
                safe[key] = value
        elif key == 'usage.currency':
            if isinstance(value, str) and re.fullmatch(r'[A-Z]{3}', value):
                safe[key] = value
    return safe
