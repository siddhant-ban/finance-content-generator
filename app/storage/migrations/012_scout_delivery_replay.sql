CREATE TABLE scout_deliveries (
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    cluster_id TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    run_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    replay_mode TEXT NOT NULL DEFAULT 'live' CHECK (replay_mode IN ('live', 'audit_replay', 'reeval')),
    source_run_id TEXT,
    trace_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (story_id, revision)
);

CREATE INDEX idx_scout_deliveries_status ON scout_deliveries(status, created_at);
CREATE INDEX idx_scout_deliveries_run_id ON scout_deliveries(run_id);

CREATE TRIGGER scout_deliveries_no_delete BEFORE DELETE ON scout_deliveries
BEGIN SELECT RAISE(ABORT, 'scout delivery records cannot be deleted'); END;

CREATE TRIGGER scout_deliveries_immutable_identity BEFORE UPDATE ON scout_deliveries
WHEN NEW.story_id != OLD.story_id
    OR NEW.revision != OLD.revision
    OR NEW.cluster_id != OLD.cluster_id
    OR NEW.artifact_sha256 != OLD.artifact_sha256
    OR NEW.run_id != OLD.run_id
    OR NEW.invocation_id != OLD.invocation_id
    OR NEW.replay_mode != OLD.replay_mode
    OR COALESCE(NEW.source_run_id, '') != COALESCE(OLD.source_run_id, '')
BEGIN SELECT RAISE(ABORT, 'scout delivery identity is immutable'); END;

CREATE TABLE scout_run_lineage (
    run_id TEXT PRIMARY KEY NOT NULL,
    invocation_id TEXT NOT NULL UNIQUE,
    replay_mode TEXT NOT NULL CHECK (replay_mode IN ('live', 'audit_replay', 'reeval')),
    source_run_id TEXT,
    source_input_sha256 TEXT REFERENCES artifacts(sha256),
    input_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    config_hash TEXT NOT NULL CHECK (length(config_hash) = 64 AND config_hash NOT GLOB '*[^0-9a-f]*'),
    prompt_hash TEXT NOT NULL CHECK (length(prompt_hash) = 64 AND prompt_hash NOT GLOB '*[^0-9a-f]*'),
    provider TEXT NOT NULL CHECK (length(provider) > 0),
    model TEXT NOT NULL CHECK (length(model) > 0),
    experiment_id TEXT,
    trace_id TEXT,
    created_at TEXT NOT NULL CHECK (length(created_at) > 0)
) WITHOUT ROWID;

CREATE INDEX idx_scout_run_lineage_source ON scout_run_lineage(source_run_id);

CREATE TRIGGER scout_run_lineage_immutable_update BEFORE UPDATE ON scout_run_lineage
BEGIN SELECT RAISE(ABORT, 'scout run lineage is immutable'); END;

CREATE TRIGGER scout_run_lineage_immutable_delete BEFORE DELETE ON scout_run_lineage
BEGIN SELECT RAISE(ABORT, 'scout run lineage is immutable'); END;

CREATE TABLE scout_run_outputs (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES scout_run_lineage(run_id),
    output_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    outcome TEXT NOT NULL CHECK (outcome IN ('complete', 'empty', 'partial', 'failed')),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0)
) WITHOUT ROWID;

CREATE TRIGGER scout_run_outputs_immutable_update BEFORE UPDATE ON scout_run_outputs
BEGIN SELECT RAISE(ABORT, 'scout run outputs are immutable'); END;

CREATE TRIGGER scout_run_outputs_immutable_delete BEFORE DELETE ON scout_run_outputs
BEGIN SELECT RAISE(ABORT, 'scout run outputs are immutable'); END;

CREATE TABLE scout_replay_inputs (
    run_id TEXT NOT NULL REFERENCES scout_run_lineage(run_id),
    item_order INTEGER NOT NULL CHECK (item_order >= 0),
    item_id TEXT NOT NULL CHECK (length(item_id) > 0),
    rights TEXT NOT NULL CHECK (rights IN ('unknown', 'metadata_only', 'excerpt_permitted', 'unparsed')),
    retained INTEGER NOT NULL CHECK (retained IN (0, 1)),
    retention_reason TEXT NOT NULL CHECK (length(retention_reason) > 0),
    source_id TEXT,
    source_revision INTEGER CHECK (source_revision IS NULL OR source_revision >= 1),
    source_record_hash TEXT CHECK (
        source_record_hash IS NULL OR (length(source_record_hash) = 64 AND source_record_hash NOT GLOB '*[^0-9a-f]*')
    ),
    canonical_url TEXT,
    payload_sha256 TEXT CHECK (
        payload_sha256 IS NULL OR (length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    PRIMARY KEY (run_id, item_order),
    UNIQUE (run_id, item_id),
    CHECK (
        (retained = 1 AND rights = 'excerpt_permitted' AND payload_json IS NOT NULL AND payload_sha256 IS NOT NULL)
        OR (retained = 0 AND payload_json IS NULL)
    )
) WITHOUT ROWID;

CREATE TRIGGER scout_replay_inputs_immutable_update BEFORE UPDATE ON scout_replay_inputs
BEGIN SELECT RAISE(ABORT, 'scout replay inputs are immutable'); END;

CREATE TRIGGER scout_replay_inputs_immutable_delete BEFORE DELETE ON scout_replay_inputs
BEGIN SELECT RAISE(ABORT, 'scout replay inputs are immutable'); END;

CREATE TABLE scout_replay_responses (
    run_id TEXT NOT NULL REFERENCES scout_run_lineage(run_id),
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    cluster_id TEXT NOT NULL,
    cluster_key TEXT NOT NULL CHECK (length(cluster_key) > 0),
    cluster_version TEXT NOT NULL CHECK (length(cluster_version) > 0),
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
    response_sha256 TEXT NOT NULL CHECK (length(response_sha256) = 64 AND response_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_json TEXT NOT NULL CHECK (json_valid(response_json)),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    candidate_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    PRIMARY KEY (run_id, story_id, revision)
) WITHOUT ROWID;

CREATE INDEX idx_scout_replay_responses_story ON scout_replay_responses(story_id, revision);

CREATE TRIGGER scout_replay_responses_immutable_update BEFORE UPDATE ON scout_replay_responses
BEGIN SELECT RAISE(ABORT, 'scout replay responses are immutable'); END;

CREATE TRIGGER scout_replay_responses_immutable_delete BEFORE DELETE ON scout_replay_responses
BEGIN SELECT RAISE(ABORT, 'scout replay responses are immutable'); END;
