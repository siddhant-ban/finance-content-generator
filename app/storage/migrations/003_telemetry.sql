CREATE TABLE telemetry_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    UNIQUE(trace_id, span_id)
);
CREATE TABLE telemetry_health (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    lost_records INTEGER NOT NULL DEFAULT 0,
    persistence_failures INTEGER NOT NULL DEFAULT 0,
    export_failures INTEGER NOT NULL DEFAULT 0,
    error_code TEXT
);
INSERT INTO telemetry_health(singleton) VALUES (1);
CREATE TABLE telemetry_degraded_runs (run_id TEXT PRIMARY KEY);
