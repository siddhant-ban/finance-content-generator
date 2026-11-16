CREATE TABLE reviewer_sessions (
    cookie_hash TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL CHECK(account_id > 0),
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1))
);
