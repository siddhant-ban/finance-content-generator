"""Disk-backed storage invariants and interrupted-write recovery."""
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts.common import ArtifactRef
from app.storage import db
from app.storage.artifacts import ArtifactStore, ArtifactIntegrityError
from app.storage.db import migrate, open_database


def setup_store(tmp_path):
    conn = open_database(tmp_path / "store.sqlite")
    migrate(conn)
    return conn, ArtifactStore(tmp_path / "artifacts", conn)


def reopen(tmp_path, conn):
    conn.close()
    return setup_store(tmp_path)


def test_same_bytes_same_ref(tmp_path):
    conn, store = setup_store(tmp_path)
    ref = store.put(b"hello", "text/plain", "1.0")
    assert ref.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert store.put(b"hello", "text/plain", "1.0") == ref
    conn, store = reopen(tmp_path, conn)
    assert store.read(ref) == b"hello"
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
    with pytest.raises(ValueError):
        store.put(b"hello", "application/json", "1.0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE artifacts SET size_bytes=2")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM artifacts")
    conn.rollback()
    conn.close()


def test_temp_write_crash_never_registers_missing_file(tmp_path, monkeypatch):
    conn, store = setup_store(tmp_path)
    prior = store.put(b"prior", "text/plain", "1.0")
    def disk_full(fd):
        raise OSError("disk full")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", disk_full)
        with pytest.raises(OSError, match="disk full"):
            store.put(b"interrupted", "text/plain", "1.0")
    conn, store = reopen(tmp_path, conn)
    assert store.read(prior) == b"prior"
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
    assert store.reconcile().missing == ()
    conn.close()


def test_rename_before_db_crash_reports_orphan(tmp_path):
    conn, store = setup_store(tmp_path)
    prior = store.put(b"prior", "text/plain", "1.0")
    conn.execute("CREATE TRIGGER simulate_failure BEFORE INSERT ON artifacts BEGIN SELECT RAISE(ABORT, 'db failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="db failure"):
        store.put(b"orphan", "text/plain", "1.0")
    conn, store = reopen(tmp_path, conn)
    report = store.reconcile()
    assert len(report.orphans) == 1
    orphan = tmp_path / "artifacts" / report.orphans[0]
    assert orphan.read_bytes() == b"orphan"
    assert store.read(prior) == b"prior"
    assert report.missing == report.corrupt == ()
    assert orphan.exists()
    conn.close()


def test_corrupt_ref_blocks_handoff(tmp_path):
    conn, store = setup_store(tmp_path)
    ref = store.put(b"hello", "text/plain", "1.0")
    target = next(path for path in (tmp_path / "artifacts").rglob("*") if path.is_file())
    target.write_bytes(b"wrong")
    with pytest.raises(ArtifactIntegrityError):
        store.read(ref)
    assert store.reconcile().corrupt == (ref,)
    target.unlink()
    conn, store = reopen(tmp_path, conn)
    with pytest.raises(ArtifactIntegrityError):
        store.read(ref)
    assert store.reconcile().missing == (ref,)
    assert not target.exists()
    conn.close()


def test_unregistered_and_forged_metadata_block_handoff(tmp_path):
    conn, store = setup_store(tmp_path)
    ref = store.put(b"hello", "text/plain", "1.0")
    for forged in (ref.model_copy(update={"size_bytes": 999}), ref.model_copy(update={"media_type": "image/png"}), ArtifactRef(sha256="0" * 64, media_type="text/plain", size_bytes=0)):
        with pytest.raises(ArtifactIntegrityError):
            store.read(forged)
    conn.close()


def test_path_traversal_and_escaping_symlink_denied(tmp_path):
    conn, store = setup_store(tmp_path)
    with pytest.raises(ValidationError):
        ArtifactRef(sha256="../outside", media_type="text/plain", size_bytes=0)
    forged = ArtifactRef.model_construct(sha256="../outside", media_type="text/plain", size_bytes=0, schema_version="1.0")
    with pytest.raises((ValueError, ValidationError)):
        store.read(forged)
    outside = tmp_path / "outside"
    outside.mkdir()
    digest = hashlib.sha256(b"hello").hexdigest()
    (tmp_path / "artifacts" / digest[:2]).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.put(b"hello", "text/plain", "1.0")
    assert list(outside.iterdir()) == []
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        ArtifactStore(linked_root, conn)
    conn.close()


def test_leaf_symlink_denied_on_read_and_write(tmp_path):
    conn, store = setup_store(tmp_path)
    ref = store.put(b"hello", "text/plain", "1.0")
    leaf = next(path for path in (tmp_path / "artifacts").rglob("*") if path.is_file())
    leaf.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"hello")
    leaf.symlink_to(outside)
    with pytest.raises(ValueError):
        store.read(ref)
    with pytest.raises(ValueError):
        store.put(b"hello", "text/plain", "1.0")
    assert store.reconcile().corrupt == (ref,)
    assert outside.read_bytes() == b"hello"
    conn.close()


def test_migrations_atomic_idempotent(tmp_path, monkeypatch):
    conn = open_database(tmp_path / "store.sqlite")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_good.sql").write_text("CREATE TABLE sentinel (value TEXT);")
    broken = migrations / "002_bad.sql"
    broken.write_text("CREATE TABLE partial (value TEXT); INVALID SQL;")
    with monkeypatch.context() as patch:
        patch.setattr(db, "MIGRATIONS_DIR", migrations)
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name IN ('sentinel','partial','schema_migrations')").fetchall() == []
    migrate(conn)
    migrate(conn)
    conn.close()
    conn = open_database(tmp_path / "store.sqlite")
    assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(path.name,) for path in sorted(db.MIGRATIONS_DIR.glob("*.sql"))]
    conn.close()


def test_foreign_keys_wal_and_timeout(tmp_path):
    conn, store = setup_store(tmp_path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.execute("CREATE TABLE child (artifact_hash TEXT REFERENCES artifacts(sha256))")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO child VALUES (?)", ("0" * 64,))
    conn.rollback()
    conn.close()


def test_orphan_adoption_syncs_before_registration(tmp_path, monkeypatch):
    conn, store = setup_store(tmp_path)
    conn.execute("CREATE TRIGGER simulate_failure BEFORE INSERT ON artifacts BEGIN SELECT RAISE(ABORT, 'db failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.put(b"orphan", "text/plain", "1.0")
    conn.execute("DROP TRIGGER simulate_failure")
    conn.commit()
    def failed_sync(fd):
        raise OSError("directory sync failed")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failed_sync)
        with pytest.raises(OSError, match="directory sync failed"):
            store.put(b"orphan", "text/plain", "1.0")
    conn, store = reopen(tmp_path, conn)
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert len(store.reconcile().orphans) == 1
    ref = store.put(b"orphan", "text/plain", "1.0")
    assert store.read(ref) == b"orphan"
    assert store.reconcile().orphans == ()
    conn.close()


def test_rename_failure_preserves_committed_content(tmp_path, monkeypatch):
    conn, store = setup_store(tmp_path)
    prior = store.put(b"prior", "text/plain", "1.0")
    def failed_rename(source, destination):
        raise OSError("rename failed")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failed_rename)
        with pytest.raises(OSError, match="rename failed"):
            store.put(b"new", "text/plain", "1.0")
    conn, store = reopen(tmp_path, conn)
    assert store.read(prior) == b"prior"
    report = store.reconcile()
    assert report.missing == report.corrupt == report.orphans == ()
    conn.close()


@pytest.mark.parametrize("fail_ancestor_sync", [False, True])
def test_nested_root_ancestors_synced_before_registration(tmp_path, monkeypatch, fail_ancestor_sync):
    conn = open_database(tmp_path / "existing.sqlite")
    migrate(conn)
    root = tmp_path / "new_a" / "new_b" / "artifacts"
    store = ArtifactStore(root, conn)
    expected_directories = (root, root.parent, root.parent.parent, tmp_path)
    identities = {(directory.stat().st_dev, directory.stat().st_ino): directory for directory in expected_directories}
    events = []
    original_sync = os.fsync
    def observed_sync(fd):
        stat = os.fstat(fd)
        directory = identities.get((stat.st_dev, stat.st_ino))
        if directory is not None:
            events.append(directory)
            if fail_ancestor_sync and directory == tmp_path:
                raise OSError("ancestor sync failed")
        original_sync(fd)
    conn.set_trace_callback(lambda statement: events.append("register") if statement.startswith("INSERT INTO artifacts") else None)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", observed_sync)
        if fail_ancestor_sync:
            with pytest.raises(OSError, match="ancestor sync failed"):
                store.put(b"nested", "text/plain", "1.0")
        else:
            ref = store.put(b"nested", "text/plain", "1.0")
    if fail_ancestor_sync:
        conn.close()
        conn = open_database(tmp_path / "existing.sqlite")
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        # A new instance must also sync ancestors before adopting the orphan.
        store = ArtifactStore(root, conn)
        events.clear()
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", observed_sync)
            with pytest.raises(OSError, match="ancestor sync failed"):
                store.put(b"nested", "text/plain", "1.0")
        assert len(store.reconcile().orphans) == 1
    else:
        assert events[:len(expected_directories)] == list(expected_directories)
        # SQLite can repeat an INSERT trace while executing its BEFORE trigger.
        registration_events = events[len(expected_directories):]
        assert registration_events and all(event == "register" for event in registration_events)
        conn.close()
        conn = open_database(tmp_path / "existing.sqlite")
        assert ArtifactStore(root, conn).read(ref) == b"nested"
    conn.close()



def test_replace_insert_cannot_mutate_artifact_metadata(tmp_path):
    conn, store = setup_store(tmp_path)
    ref = store.put(b"hello", "text/plain", "1.0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO artifacts (sha256, media_type, size_bytes, schema_version) VALUES (?, ?, ?, ?)",
            (ref.sha256, "image/png", 2, "1.0"),
        )
    conn.rollback()
    conn, store = reopen(tmp_path, conn)
    assert conn.execute("SELECT media_type, size_bytes FROM artifacts WHERE sha256 = ?", (ref.sha256,)).fetchone() == ("text/plain", 5)
    assert store.read(ref) == b"hello"
    conn.close()
