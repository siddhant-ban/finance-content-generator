CREATE TABLE scout_editorial_decisions (
    decision_id TEXT PRIMARY KEY NOT NULL,
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
    candidate_sha256 TEXT NOT NULL CHECK(length(candidate_sha256) = 64 AND candidate_sha256 NOT GLOB '*[^0-9a-f]*'),
    decision TEXT NOT NULL CHECK(decision IN ('assigned', 'passed')),
    reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) > 0),
    notes TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL CHECK(length(trim(actor_id)) > 0),
    decided_at TEXT NOT NULL CHECK(length(trim(decided_at)) > 0),
    FOREIGN KEY (story_id, revision) REFERENCES scout_story_revisions(story_id, revision),
    FOREIGN KEY (candidate_sha256) REFERENCES artifacts(sha256)
) WITHOUT ROWID;

CREATE INDEX idx_scout_editorial_decisions_story
    ON scout_editorial_decisions(story_id, revision, decided_at);
CREATE INDEX idx_scout_editorial_decisions_run
    ON scout_editorial_decisions(run_id);
CREATE INDEX idx_scout_editorial_decisions_decided_at
    ON scout_editorial_decisions(decided_at);

CREATE TRIGGER scout_editorial_decisions_no_update BEFORE UPDATE ON scout_editorial_decisions
BEGIN SELECT RAISE(ABORT, 'scout editorial decisions are append-only'); END;
CREATE TRIGGER scout_editorial_decisions_no_delete BEFORE DELETE ON scout_editorial_decisions
BEGIN SELECT RAISE(ABORT, 'scout editorial decisions are append-only'); END;
CREATE TRIGGER scout_editorial_decisions_no_replace BEFORE INSERT ON scout_editorial_decisions
WHEN EXISTS (SELECT 1 FROM scout_editorial_decisions WHERE decision_id = NEW.decision_id)
BEGIN SELECT RAISE(ABORT, 'scout editorial decisions are append-only'); END;

CREATE TABLE scout_pilot_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    recorded_at TEXT NOT NULL CHECK(length(trim(recorded_at)) > 0),
    outcome TEXT NOT NULL CHECK(outcome IN ('complete', 'empty', 'partial', 'failed')),
    feed_ids_json TEXT NOT NULL CHECK(json_valid(feed_ids_json)),
    candidate_feeds_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(candidate_feeds_json)),
    experiment_id TEXT,
    example_id TEXT,
    reservation_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reservation_ids_json)),
    reservation_cost_kinds_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(reservation_cost_kinds_json)),
    cost_records_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(cost_records_json))
) WITHOUT ROWID;

CREATE INDEX idx_scout_pilot_runs_recorded_at
    ON scout_pilot_runs(recorded_at);

CREATE TRIGGER scout_pilot_runs_no_update BEFORE UPDATE ON scout_pilot_runs
BEGIN SELECT RAISE(ABORT, 'scout pilot runs are append-only'); END;
CREATE TRIGGER scout_pilot_runs_no_delete BEFORE DELETE ON scout_pilot_runs
BEGIN SELECT RAISE(ABORT, 'scout pilot runs are append-only'); END;
CREATE TRIGGER scout_pilot_runs_no_replace BEFORE INSERT ON scout_pilot_runs
WHEN EXISTS (SELECT 1 FROM scout_pilot_runs WHERE run_id = NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'scout pilot runs are append-only'); END;
