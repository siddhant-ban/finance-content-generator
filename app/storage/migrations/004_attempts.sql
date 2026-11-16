CREATE TABLE execution_requests (
    request_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    input_envelope TEXT NOT NULL,
    configuration TEXT NOT NULL
);
CREATE TABLE execution_attempts (
    invocation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES execution_requests(request_id),
    retry_count INTEGER NOT NULL CHECK(retry_count BETWEEN 0 AND 2),
    status TEXT NOT NULL CHECK(status IN ('STARTED','OUTPUT_STORED','COMPLETED','FAILED','BLOCKED','INTERRUPTED')),
    payload TEXT NOT NULL,
    UNIQUE(request_id,retry_count)
);
