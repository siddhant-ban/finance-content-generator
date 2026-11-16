"""Fail-closed local dataset loading with global integrity and split checks."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.evals.models import DatasetManifest, EvalExample, LoadedDataset, validate_provenance

SPLITS = ('dev', 'holdout', 'prod_regression')


class DatasetValidationError(ValueError):
    """Safe error code/location only; never includes row values or schema errors."""


def _fail(path: Path, code: str, line: int = 0) -> None:
    raise DatasetValidationError(f'{path.name}:{line}:{code}') from None


def _safe_file(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 1:
        _fail(root / 'manifest.json', 'unsafe_path')
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        _fail(root / 'manifest.json', 'unsafe_path')
    return path


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        _fail(path, 'unreadable_file')


@dataclass(frozen=True, repr=False)
class DatasetSnapshot:
    """Private immutable source bytes; never include in routine projections."""
    manifest_bytes: bytes
    split_bytes: tuple[tuple[str, bytes], ...]


def _manifest(raw: bytes) -> DatasetManifest:
    path=Path('manifest.json')
    try:
        header=json.loads(raw)
    except (ValueError,UnicodeError):
        _fail(path,'invalid_manifest')
    if isinstance(header,dict) and header.get('kind')=='illustrative':
        _fail(path,'illustrative_not_promotable')
    try:
        return DatasetManifest.model_validate_json(raw)
    except (ValidationError,ValueError):
        _fail(path,'invalid_manifest')


def read_dataset_snapshot(root: Path) -> DatasetSnapshot:
    """Capture each filesystem artifact once; validation consumes these exact bytes."""
    raw=_read(_safe_file(root,'manifest.json'))
    manifest=_manifest(raw)
    contents=tuple((name,_read(_safe_file(root,manifest.files[name].path))) for name in SPLITS)
    return DatasetSnapshot(raw,contents)


def load_dataset_snapshot(
    snapshot: DatasetSnapshot,
    split: Literal['dev', 'holdout', 'prod_regression'],
    purpose: Literal['tune', 'evaluate', 'gate'],
) -> LoadedDataset:
    """Pure shared global validation for filesystem and complete offline snapshots."""
    if split not in SPLITS or purpose not in ('tune','evaluate','gate'):
        raise DatasetValidationError('invalid_selection')
    if split=='holdout' and purpose=='tune':
        raise DatasetValidationError('holdout_tune_denied')
    manifest_path=Path('manifest.json')
    manifest=_manifest(snapshot.manifest_bytes)
    supplied=dict(snapshot.split_bytes)
    if len(snapshot.split_bytes)!=len(SPLITS) or set(supplied)!=set(SPLITS):
        _fail(manifest_path,'required_split_files')
    contents={}
    for name in SPLITS:
        pin=manifest.files[name]
        path=Path(pin.path)
        if path.is_absolute() or '..' in path.parts or len(path.parts)!=1:
            _fail(manifest_path,'unsafe_path')
        content=supplied[name]
        if hashlib.sha256(content).hexdigest()!=pin.sha256:
            _fail(path,'file_hash_mismatch')
        contents[name]=(path,content)
    if len({pin.path for pin in manifest.files.values()})!=len(SPLITS):
        _fail(manifest_path,'duplicate_split_file')

    examples = {}
    seen_ids = set()
    event_splits = {}
    evidence_splits = {}
    for name in SPLITS:
        path, content = contents[name]
        rows = []
        for number, line in enumerate(content.splitlines(), 1):
            try:
                row = EvalExample.model_validate_json(line)
                validate_provenance(manifest.kind, row.rights, row.adjudication)
            except (ValidationError, ValueError):
                _fail(path, 'invalid_example', number)
            if row.example_id in seen_ids:
                _fail(path, 'duplicate_example_id', number)
            seen_ids.add(row.example_id)
            if (row.agent_name, row.agent_version, row.dataset_version, row.split) != (
                manifest.agent_name, manifest.agent_version, manifest.dataset_version, name
            ):
                _fail(path, 'identity_mismatch', number)
            if row.labels.target_behavior not in manifest.label_taxonomy:
                _fail(path, 'unknown_label', number)
            if row.event_group in event_splits and event_splits[row.event_group] != name:
                _fail(path, 'event_leakage', number)
            event_splits[row.event_group] = name
            for fingerprint in row.evidence_fingerprints:
                if fingerprint in evidence_splits and evidence_splits[fingerprint] != name:
                    _fail(path, 'evidence_leakage', number)
                evidence_splits[fingerprint] = name
            rows.append(row)
        if purpose == 'gate' and not rows:
            _fail(path, 'empty_required_split')
        examples[name] = tuple(rows)

    for name, rows in examples.items():
        path, _ = contents[name]
        pin = manifest.files[name]
        if tuple(row.example_id for row in rows) != pin.example_ids:
            _fail(path, 'membership_mismatch')
        if set(row.event_group for row in rows) != set(pin.event_groups):
            _fail(path, 'event_membership_mismatch')
        if set(fp for row in rows for fp in row.evidence_fingerprints) != set(pin.evidence_fingerprints):
            _fail(path, 'evidence_membership_mismatch')
        for number, row in enumerate(rows, 1):
            if any(ref not in seen_ids for ref in row.reference_ids):
                _fail(path, 'unresolved_reference_id', number)

    loaded = LoadedDataset(
        dataset_version=manifest.dataset_version, dataset_hash=manifest.record_hash(),
        agent_name=manifest.agent_name, kind=manifest.kind, split=split,
        purpose=purpose, count=len(examples[split]),
    )
    loaded._examples = examples[split]
    return loaded


def load_dataset(
    root: Path,
    split: Literal['dev', 'holdout', 'prod_regression'],
    purpose: Literal['tune', 'evaluate', 'gate'],
) -> LoadedDataset:
    """Validate every split from one captured snapshot before exposing selected rows."""
    # Preserve selection errors without touching the filesystem.
    if split not in SPLITS or purpose not in ('tune','evaluate','gate'):
        raise DatasetValidationError('invalid_selection')
    if split=='holdout' and purpose=='tune':
        raise DatasetValidationError('holdout_tune_denied')
    loaded=load_dataset_snapshot(read_dataset_snapshot(root),split,purpose)
    loaded._source_root=root.resolve()
    return loaded
