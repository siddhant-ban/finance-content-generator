CREATE TABLE scout_run_requests (
    run_id TEXT PRIMARY KEY NOT NULL,
    invocation_id TEXT NOT NULL UNIQUE,
    input_sha256 TEXT NOT NULL REFERENCES artifacts(sha256)
);
