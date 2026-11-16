"""Conservative local retention. Business history and queued telemetry are never deleted."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Literal

from app.config import Settings
from app.contracts.common import StrictModel
from app.evals.models import ExperimentBundle
from app.evals.run import read_bundle, validate_bundle
from app.release.models import ReleaseCandidate


class RetentionRef(StrictModel):
    ref: str
    reason: str
    size_bytes: int


class RetentionReport(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    dry_run: bool
    eligible: tuple[RetentionRef, ...] = ()
    deleted: tuple[RetentionRef, ...] = ()
    protected: tuple[RetentionRef, ...] = ()
    eligible_bytes: int = 0
    deleted_bytes: int = 0
    errors: tuple[str, ...] = ()


# These are the local layouts created by the foundation runtime and eval CLI.
DATABASES = ('state.db', 'telemetry.db', 'evals/runtime/state.db', 'evals/runtime/telemetry.db')
JSON_COLUMNS = {'payload', 'response', 'receipt', 'candidate', 'evidence', 'input_envelope', 'configuration'}

# Supported foundation schema through migration 007. An absent empty history
# table is still missing authority, rather than proof of absent references.
BUSINESS_SCHEMA = {
    'approval_decisions': 'decision_id story_id manifest_hash story_version principal_id',
    'artifacts': 'sha256 media_type size_bytes schema_version',
    'execution_attempts': 'invocation_id request_id retry_count status payload',
    'execution_requests': 'request_id fingerprint input_envelope configuration',
    'feedback_quarantine': 'feedback_id receipt',
    'human_decisions': 'decision_id story_id payload',
    'release_approvals': 'approval_id agent_name candidate_hash version receipt',
    'release_candidates': 'candidate_hash candidate evidence',
    'release_events': 'event_id operation agent_name version candidate_hash occurred_at',
    'release_heads': 'agent_name version active_id baseline_id',
    'release_receipts': 'scope idempotency_key fingerprint approval_id',
    'release_reviewers': 'approval_id reviewer_provider reviewer_id',
    'review_receipts': 'scope idempotency_key fingerprint decision_id',
    'reviewer_sessions': 'cookie_hash session_id account_id created last_seen expires revoked',
    'schema_migrations': 'version',
    'state_events': 'event_id story_id version reason_code payload',
    'state_receipts': 'caller_scope operation idempotency_key fingerprint response',
    'stories': 'story_id run_id state version payload manifest_hash approval_id resume_target',
}
TELEMETRY_SCHEMA = {
    'telemetry_outbox': 'id trace_id span_id payload size_bytes',
    'telemetry_health': 'singleton lost_records persistence_failures export_failures error_code',
    'telemetry_degraded_runs': 'run_id',
}



def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _references(connections):
    """Read every historical row; revoked heads do not erase reference protection."""
    refs = set()
    business_seen = False
    for conn in connections:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        database = Path(conn.execute('PRAGMA database_list').fetchone()[2])
        business = database.name == 'state.db'
        expected = {**BUSINESS_SCHEMA, **TELEMETRY_SCHEMA} if business else TELEMETRY_SCHEMA
        for table, names in expected.items():
            if table not in tables:
                raise ValueError('reference_metadata_unavailable')
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if not set(names.split()).issubset(columns):
                raise ValueError('reference_metadata_unavailable')
        business_seen = business_seen or business
        for table in tables:
            # Identifiers originate in SQLite schema, but still quote them.
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info({quoted})')]
            for row in conn.execute(f'SELECT * FROM {quoted}'):
                values = dict(zip(columns, row))
                for name, value in values.items():
                    if value is None:
                        continue
                    if name in JSON_COLUMNS:
                        decoded = json.loads(value)
                        if not isinstance(decoded, (dict, list)):
                            raise ValueError('reference_metadata_unavailable')
                        refs.update(_strings(decoded))
                    elif isinstance(value, str):
                        refs.add(value)
                if table == 'release_candidates':
                    candidate = ReleaseCandidate.model_validate_json(values['candidate'])
                    if candidate.record_hash() != values['candidate_hash'] or set(candidate.evidence) != {'dev', 'holdout', 'prod_regression'}:
                        raise ValueError('reference_metadata_unavailable')
                    evidence = json.loads(values['evidence'])
                    if set(evidence) != set(candidate.evidence):
                        raise ValueError('reference_metadata_unavailable')
                    for split, pin in candidate.evidence.items():
                        bundle = validate_bundle(ExperimentBundle.model_validate_json(json.dumps(evidence[split])))
                        if bundle.record_hash() != pin.bundle_hash:
                            raise ValueError('reference_metadata_unavailable')
                        path = Path(pin.path)
                        if path.is_symlink():
                            raise ValueError('reference_metadata_unavailable')
                        if path.is_dir():
                            from app.evals.phoenix import load_offline
                            external = load_offline(path).bundle
                        else:
                            external = read_bundle(path)
                        if external.record_hash() != pin.bundle_hash:
                            raise ValueError('reference_metadata_unavailable')
    if not business_seen:
        raise ValueError('reference_metadata_unavailable')
    return refs


def _entry(path, root, reason):
    try:
        size = path.lstat().st_size
    except OSError:
        return RetentionRef(ref=str(path.relative_to(root)),
                            reason='filesystem_unavailable', size_bytes=0)
    return RetentionRef(ref=str(path.relative_to(root)), reason=reason, size_bytes=size)


def _referenced(path, bundle, refs):
    return (bundle.record_hash() in refs or str(path) in refs or str(path.resolve()) in refs
            or any(path.resolve().is_relative_to(Path(r).resolve())
                   for r in refs if r and '\x00' not in r))


def cleanup(now: datetime, dry_run: bool = True, *, data_dir: Path | None = None) -> RetentionReport:
    """Remove strictly older-than-30-day completed canonical eval files only.

    Default source reads use immutable SQLite handles without creating sidecars.
    A nonempty WAL makes dry-run reference authority uncertain: retain everything.
    Explicit execution holds every supported database's write reservation through
    reference recheck and unlink; no database records are updated or deleted.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware_now_required')
    root = (data_dir if data_dir is not None else Settings.from_env().data_dir).absolute()
    eligible, deleted, protected, errors = [], [], [], []
    candidates = []
    try:
        if not root.exists():
            return RetentionReport(dry_run=dry_run)
        # Enumerate only supported roots, reporting other trees as protected units.
        paths = sorted([p for p in root.iterdir() if p.name != 'evals']
                       + (list((root/'evals').iterdir())
                          if (root/'evals').is_dir() and not (root/'evals').is_symlink()
                          else ([root/'evals'] if (root/'evals').exists() else [])))
        # Never traverse a configured symlink root or delete through a symlink parent.
        unsafe_root = root.is_symlink() or any(p.is_symlink() for p in root.parents)
        for path in paths:
            metadata = path.lstat()  # Inspect before opening any candidate bytes.
            if path.is_dir() and not path.is_symlink():
                protected.append(_entry(path, root, 'protected_tree'))
                continue
            if (not stat.S_ISREG(metadata.st_mode) or unsafe_root or path.is_symlink() or any(p.is_symlink() for p in path.parents)
                    or path.parent != root/'evals' or not re.fullmatch('[0-9a-f]{64}\\.json', path.name)):
                protected.append(_entry(path, root, 'immutable_or_pending_or_unsupported'))
                continue
            try:
                bundle = read_bundle(path)
                times = [r.finished_at for r in bundle.records]
                if bundle.status in {'INCOMPLETE', 'ERROR'} or not times or any(t is None for t in times):
                    protected.append(_entry(path, root, 'completion_unproven'))
                elif max(times) >= now-timedelta(days=30):
                    protected.append(_entry(path, root, 'within_retention'))
                else:
                    candidates.append((path, bundle, path.read_bytes()))
            except Exception:
                protected.append(_entry(path, root, 'invalid_bundle'))
                errors.append('invalid_bundle')
    except OSError:
        # Discovery/stat uncertainty aborts eligibility before any unlink.
        uncertain = RetentionRef(ref='.', reason='filesystem_unavailable', size_bytes=0)
        pending = tuple(RetentionRef(ref=str(path.relative_to(root)),
            reason='filesystem_unavailable', size_bytes=len(raw)) for path, _, raw in candidates)
        return RetentionReport(dry_run=dry_run, protected=tuple(protected) + pending + (uncertain,),
                               errors=('filesystem_unavailable',))
    if candidates:
        try:
            with ExitStack() as stack:
                connections = []
                for name in DATABASES:
                    path = root/name
                    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                        raise ValueError('reference_metadata_unavailable')
                    if not path.exists():
                        continue
                    wal = Path(str(path)+'-wal')
                    if dry_run and wal.exists() and wal.stat().st_size:
                        raise ValueError('reference_metadata_unavailable')
                    uri = path.as_uri() + ('?mode=ro&immutable=1' if dry_run else '?mode=rw')
                    conn = sqlite3.connect(uri, uri=True, timeout=0.1)
                    stack.callback(conn.close)
                    conn.execute('BEGIN' if dry_run else 'BEGIN IMMEDIATE')
                    connections.append(conn)
                if not connections:
                    raise ValueError('reference_metadata_unavailable')
                refs = _references(connections)
                for path, bundle, captured in candidates:
                    # Re-read authoritative references inside the transaction immediately
                    # before each unlink, including any historically pinned evidence.
                    refs = _references(connections)
                    if _referenced(path, bundle, refs):
                        protected.append(_entry(path, root, 'referenced_history'))
                        continue
                    if path.is_symlink() or path.read_bytes() != captured:
                        protected.append(_entry(path, root, 'file_changed'))
                        errors.append('file_changed')
                        continue
                    entry = _entry(path, root, 'completed_unreferenced_older_than_30_days')
                    eligible.append(entry)
                    if not dry_run:
                        try:
                            path.unlink()
                            deleted.append(entry)
                        except OSError:
                            errors.append('deletion_failed')
        except Exception:
            errors.append('reference_metadata_unavailable')
            decided = {r.ref for r in (*eligible, *protected)}
            for path, _, _ in candidates:
                if str(path.relative_to(root)) not in decided:
                    protected.append(_entry(path, root, 'reference_metadata_unavailable'))
    return RetentionReport(dry_run=dry_run, eligible=tuple(eligible), deleted=tuple(deleted),
        protected=tuple(sorted(protected, key=lambda r:r.ref)), errors=tuple(sorted(set(errors))),
        eligible_bytes=sum(r.size_bytes for r in eligible), deleted_bytes=sum(r.size_bytes for r in deleted))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    report = cleanup(datetime.now(timezone.utc), dry_run=not args.execute)
    print(report.model_dump_json())
    return 2 if report.errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
