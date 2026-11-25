CREATE TABLE scout_eval_experiments (
    experiment_id TEXT PRIMARY KEY NOT NULL,
    hard_spend_cap_pico INTEGER NOT NULL CHECK(hard_spend_cap_pico >= 0),
    hard_spend_cap_usd TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE scout_eval_reservations (
    reservation_id TEXT PRIMARY KEY NOT NULL,
    experiment_id TEXT NOT NULL REFERENCES scout_eval_experiments(experiment_id),
    profile_name TEXT NOT NULL,
    model_id TEXT NOT NULL,
    example_id TEXT NOT NULL,
    attempt_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'reconciled', 'ambiguous', 'released')),
    reserved_pico INTEGER NOT NULL CHECK(reserved_pico >= 0),
    charged_pico INTEGER CHECK(charged_pico IS NULL OR charged_pico >= 0),
    reserved_usd TEXT NOT NULL,
    charged_usd TEXT,
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
    resolved_model TEXT,
    failure_disposition TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(experiment_id, attempt_key)
) WITHOUT ROWID;

CREATE INDEX idx_scout_eval_reservations_exp_status
    ON scout_eval_reservations(experiment_id, status);
