"""Dedicated SQLite outbox; every operation owns a short independent connection."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import UUID
from app.observability.models import SpanRecord, EnqueueResult

class Outbox:
    def __init__(self, path: Path, max_records: int = 10_000, max_bytes: int = 52_428_800):
        self.path = path
        if not isinstance(path, Path) or max_records < 1 or max_bytes < 1:
            raise ValueError('local Path and positive telemetry limits required')
        self.max_records = min(max_records, 10_000)
        self.max_bytes = min(max_bytes, 52_428_800)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=0.02)
        try:
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA busy_timeout=20')
            yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.connection() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            # Dedicated telemetry database: no business tables or connection.
            conn.execute('BEGIN IMMEDIATE')
            try:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='telemetry_outbox'").fetchone()
                if not exists:
                    statement = ''
                    sql = (Path(__file__).parents[1]/'storage/migrations/003_telemetry.sql').read_text()
                    for char in sql:
                        statement += char
                        if char == ';' and sqlite3.complete_statement(statement):
                            conn.execute(statement); statement = ''
                conn.commit()
            except BaseException:
                conn.rollback(); raise

    @staticmethod
    def _mark_runs(conn, runs):
        conn.executemany('INSERT OR IGNORE INTO telemetry_degraded_runs VALUES (?)',
                         [(str(run),) for run in runs if run is not None])

    def enqueue(self, record: SpanRecord) -> EnqueueResult:
        payload = record.canonical_bytes()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                existing = conn.execute('SELECT payload FROM telemetry_outbox WHERE trace_id=? AND span_id=?', (record.trace_id, record.span_id)).fetchone()
                count, size = conn.execute('SELECT count(*), coalesce(sum(size_bytes),0) FROM telemetry_outbox').fetchone()
                error = None
                if existing:
                    if existing[0].encode() != payload:
                        error = 'duplicate_conflict'
                elif count >= self.max_records or size + len(payload) > self.max_bytes:
                    error = 'capacity_exceeded'
                else:
                    conn.execute('INSERT INTO telemetry_outbox(trace_id,span_id,payload,size_bytes) VALUES (?,?,?,?)',
                                 (record.trace_id,record.span_id,payload.decode(),len(payload)))
                if error:
                    conn.execute('UPDATE telemetry_health SET lost_records=lost_records+1,error_code=?', (error,))
                    self._mark_runs(conn, (record.run_id,))
                conn.commit()
                return EnqueueResult(accepted=error is None,error_code=error)
            except BaseException:
                conn.rollback(); raise

    def pending(self, limit: int | None = None):
        with self.connection() as conn:
            sql = 'SELECT id,payload FROM telemetry_outbox ORDER BY id'
            rows = conn.execute(sql if limit is None else sql+' LIMIT ?', () if limit is None else (limit,)).fetchall()
        return [(row[0], SpanRecord.model_validate_json(row[1])) for row in rows]

    def acknowledge(self, ids):
        with self.connection() as conn:
            with conn:
                conn.executemany('DELETE FROM telemetry_outbox WHERE id=?', [(item,) for item in ids])
                conn.execute("UPDATE telemetry_health SET error_code=NULL WHERE error_code IN ('export_failed','export_timeout')")

    def failure(self, code, runs=(), lost=False):
        with self.connection() as conn:
            with conn:
                column = 'persistence_failures' if code == 'persistence_failed' else 'export_failures'
                conn.execute(f'UPDATE telemetry_health SET {column}={column}+1,lost_records=lost_records+?,error_code=?', (int(lost),code))
                self._mark_runs(conn, runs)

    def stats(self):
        with self.connection() as conn:
            count,size = conn.execute('SELECT count(*),coalesce(sum(size_bytes),0) FROM telemetry_outbox').fetchone()
            lost,persistence,exports,error = conn.execute('SELECT lost_records,persistence_failures,export_failures,error_code FROM telemetry_health WHERE singleton=1').fetchone()
            runs = tuple(UUID(row[0]) for row in conn.execute('SELECT run_id FROM telemetry_degraded_runs ORDER BY run_id'))
        return dict(pending_count=count,pending_bytes=size,lost_records=lost,persistence_failures=persistence,export_failures=exports,error_code=error,degraded_runs=runs)
