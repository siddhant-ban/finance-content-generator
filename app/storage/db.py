"""SQLite configuration and atomic ordered migrations."""
from pathlib import Path
import sqlite3

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def open_database(path: Path) -> sqlite3.Connection:
    """Open a local database; the caller owns its connection and transactions."""
    if not isinstance(path, Path):
        raise TypeError("database path must be a local Path")
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = FULL")
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending SQL files in one transaction, including version recording."""
    if conn.in_transaction:
        raise ValueError("migration requires an idle connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            # executescript commits implicitly; execute complete statements instead.
            statement = ""
            for line in path.read_text().splitlines(keepends=True):
                for char in line:
                    statement += char
                    if char == ";" and sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ""
            if statement.strip():
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (path.name,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
