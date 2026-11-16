"""Dataset governance: mutations must never bypass global integrity or holdout privacy."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path('evals/foundation/1.0.0')


def loader():
    from app.evals.datasets import load_dataset
    return load_dataset


def copy_dataset(tmp_path):
    shutil.copytree(ROOT, tmp_path / 'dataset')
    return tmp_path / 'dataset'


def rewrite(root, split, mutate):
    path = root / f'{split}.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    mutate(rows)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['files'][split]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest['files'][split]['example_ids'] = [row['example_id'] for row in rows]
    (root / 'manifest.json').write_text(json.dumps(manifest))


def test_registered_typed_fixture_dataset():
    data = loader()(ROOT, 'dev', 'gate')
    assert data.summary()['count'] >= 2
    examples = tuple(data.iter_examples())
    assert examples[0].input.data.value == 1
    assert examples[0].reference.output.value == 2
    assert any(e.severity == 'critical' and e.reference.outcome == 'reject' for e in examples)


def test_duplicate_example_ids(tmp_path):
    root = copy_dataset(tmp_path)
    rewrite(root, 'dev', lambda rows: rows.append(rows[0]))
    with pytest.raises(ValueError, match='duplicate_example_id'):
        loader()(root, 'dev', 'evaluate')


@pytest.mark.parametrize('field,code', [('event_group', 'event_leakage'), ('evidence_fingerprints', 'evidence_leakage')])
def test_event_or_evidence_leaks_across_splits(tmp_path, field, code):
    root = copy_dataset(tmp_path)
    source = json.loads((root / 'dev.jsonl').read_text().splitlines()[0])
    rewrite(root, 'holdout', lambda rows: rows[0].update({field: source[field]}))
    with pytest.raises(ValueError, match=code):
        loader()(root, 'dev', 'evaluate')


def test_file_hash_changed_version_rejected(tmp_path):
    root = copy_dataset(tmp_path)
    with (root / 'holdout.jsonl').open('a') as stream:
        stream.write('\n')
    with pytest.raises(ValueError, match='file_hash_mismatch'):
        loader()(root, 'dev', 'evaluate')


def test_empty_required_split_blocks_gate(tmp_path):
    root = copy_dataset(tmp_path)
    rewrite(root, 'holdout', lambda rows: rows.clear())
    with pytest.raises(ValueError, match='empty_required_split'):
        loader()(root, 'dev', 'gate')


@pytest.mark.parametrize('field', ['adjudication', 'rights'])
def test_missing_adjudication_or_rights(tmp_path, field):
    root = copy_dataset(tmp_path)
    rewrite(root, 'prod_regression', lambda rows: rows[0].pop(field))
    with pytest.raises(ValueError, match=r'prod_regression.jsonl:1:invalid_example'):
        loader()(root, 'dev', 'evaluate')


def test_illustrative_never_promotable():
    with pytest.raises(ValueError, match='illustrative_not_promotable'):
        loader()(Path('evals/golden'), 'dev', 'gate')


def test_holdout_tune_denied():
    with pytest.raises(ValueError, match='holdout_tune_denied'):
        loader()(ROOT, 'holdout', 'tune')


def test_holdout_report_contains_no_examples():
    data = loader()(ROOT, 'holdout', 'evaluate')
    for report in (repr(data), str(data), json.dumps(data.summary()), data.model_dump_json(), str(data.model_dump())):
        assert 'foundation-holdout' not in report
        assert 'critical_trap' not in report
        assert 'event_group' not in report
        assert 'reference' not in report
    assert len(tuple(data.iter_examples())) >= 2


@pytest.mark.parametrize('split,purpose', [('bad', 'gate'), ('dev', 'bad')])
def test_unknown_selection_rejected(split, purpose):
    with pytest.raises(ValueError, match='invalid_selection'):
        loader()(ROOT, split, purpose)


@pytest.mark.parametrize('change', ['unknown_agent', 'unknown_schema', 'unresolved_ref', 'bad_payload', 'membership', 'version', 'unsafe_path'])
def test_global_contract_failures(tmp_path, change):
    root = copy_dataset(tmp_path)
    if change == 'unsafe_path':
        manifest = json.loads((root / 'manifest.json').read_text())
        manifest['files']['holdout']['path'] = '../outside.jsonl'
        (root / 'manifest.json').write_text(json.dumps(manifest))
    elif change == 'membership':
        manifest = json.loads((root / 'manifest.json').read_text())
        manifest['files']['holdout']['example_ids'] = ['missing']
        (root / 'manifest.json').write_text(json.dumps(manifest))
    else:
        edits = {'unknown_agent': {'agent_name': 'unknown'}, 'unknown_schema': {'schema_version': '99'}, 'unresolved_ref': {'reference_ids': ['missing']}, 'bad_payload': {'input': {'data': {'value': 'secret-row-value'}}}, 'version': {'dataset_version': '2.0.0'}}
        rewrite(root, 'holdout', lambda rows: rows[0].update(edits[change]))
    codes = {'unknown_agent': 'invalid_example', 'unknown_schema': 'invalid_example', 'unresolved_ref': 'unresolved_reference_id', 'bad_payload': 'invalid_example', 'membership': 'membership_mismatch', 'version': 'identity_mismatch', 'unsafe_path': 'unsafe_path'}
    with pytest.raises(ValueError, match=codes[change]) as error:
        loader()(root, 'dev', 'evaluate')
    assert 'secret-row-value' not in str(error.value)


def test_malformed_holdout_has_safe_line_error(tmp_path):
    root = copy_dataset(tmp_path)
    path = root / 'holdout.jsonl'
    path.write_text('{"private-secret": invalid}\n')
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['files']['holdout']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='holdout.jsonl:1:invalid_example') as error:
        loader()(root, 'dev', 'evaluate')
    assert 'private-secret' not in str(error.value)


def test_symlink_escape_rejected(tmp_path):
    root = copy_dataset(tmp_path)
    target = tmp_path / 'external'
    shutil.copy(root / 'holdout.jsonl', target)
    (root / 'holdout.jsonl').unlink()
    (root / 'holdout.jsonl').symlink_to(target)
    with pytest.raises(ValueError, match='unsafe_path'):
        loader()(root, 'dev', 'evaluate')


@pytest.mark.parametrize('field,value,code', [
    ('rights', {'status': 'reviewed_permitted', 'provenance': 'permitted', 'retention': 'local'}, 'invalid_manifest'),
    ('kind', 'adjudicated_agent', 'invalid_manifest'),
    ('agent_name', 'news_scout', 'invalid_manifest'),
    ('input_schema', 'unknown.input/1.0', 'invalid_manifest'),
])
def test_manifest_provenance_and_registry_rejected(tmp_path, field, value, code):
    root = copy_dataset(tmp_path)
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest[field] = value
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=code):
        loader()(root, 'dev', 'gate')


def test_model_only_adjudication_cannot_be_ground_truth(tmp_path):
    root = copy_dataset(tmp_path)
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['kind'] = 'adjudicated_agent'
    manifest['rights']['status'] = 'reviewed_permitted'
    manifest['adjudication']['status'] = 'human_adjudicated'
    manifest['adjudication']['labeler_kind'] = 'model'
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='invalid_manifest'):
        loader()(root, 'dev', 'gate')


def test_reference_reject_cannot_masquerade_as_accept(tmp_path):
    root = copy_dataset(tmp_path)
    rewrite(root, 'holdout', lambda rows: rows[1].update(reference={'outcome':'accept','output':{'value':3},'rejection_code':None}))
    with pytest.raises(ValueError, match='holdout.jsonl:2:invalid_example'):
        loader()(root, 'dev', 'gate')


def test_critical_trap_labels_require_rejection(tmp_path):
    root = copy_dataset(tmp_path)
    rewrite(root, 'holdout', lambda rows: rows[0].update(labels={'target_behavior':'increment','critical_trap':True}))
    with pytest.raises(ValueError, match='holdout.jsonl:1:invalid_example'):
        loader()(root, 'dev', 'gate')


def test_holdout_report_excludes_explicit_example_include():
    data = loader()(ROOT, 'holdout', 'evaluate')
    assert data.model_dump(include={'_examples'}) == {}


def test_blank_row_is_not_silently_skipped(tmp_path):
    root = copy_dataset(tmp_path)
    path = root / 'holdout.jsonl'
    path.write_bytes(path.read_bytes() + b'\n')
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['files']['holdout']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='holdout.jsonl:4:invalid_example'):
        loader()(root, 'dev', 'evaluate')
