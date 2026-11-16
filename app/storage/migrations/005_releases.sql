CREATE TABLE release_candidates (
    candidate_hash TEXT PRIMARY KEY,
    candidate TEXT NOT NULL,
    evidence TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE release_approvals (
    approval_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    candidate_hash TEXT NOT NULL REFERENCES release_candidates(candidate_hash),
    version INTEGER NOT NULL,
    receipt TEXT NOT NULL,
    UNIQUE(agent_name,version)
) WITHOUT ROWID;
CREATE TABLE release_heads (
    agent_name TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    active_id TEXT REFERENCES release_approvals(approval_id),
    baseline_id TEXT NOT NULL REFERENCES release_approvals(approval_id)
);
CREATE TABLE release_receipts (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    approval_id TEXT NOT NULL REFERENCES release_approvals(approval_id),
    PRIMARY KEY(scope,idempotency_key)
) WITHOUT ROWID;
CREATE TABLE release_events (
    event_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    candidate_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE feedback_quarantine (
    feedback_id TEXT PRIMARY KEY,
    receipt TEXT NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER release_candidates_no_update BEFORE UPDATE ON release_candidates BEGIN SELECT RAISE(ABORT,'immutable release candidate'); END;
CREATE TRIGGER release_candidates_no_delete BEFORE DELETE ON release_candidates BEGIN SELECT RAISE(ABORT,'immutable release candidate'); END;
CREATE TRIGGER release_approvals_no_update BEFORE UPDATE ON release_approvals BEGIN SELECT RAISE(ABORT,'immutable release approval'); END;
CREATE TRIGGER release_approvals_no_delete BEFORE DELETE ON release_approvals BEGIN SELECT RAISE(ABORT,'immutable release approval'); END;
CREATE TRIGGER release_receipts_no_update BEFORE UPDATE ON release_receipts BEGIN SELECT RAISE(ABORT,'immutable release receipt'); END;
CREATE TRIGGER release_receipts_no_delete BEFORE DELETE ON release_receipts BEGIN SELECT RAISE(ABORT,'immutable release receipt'); END;
CREATE TRIGGER release_events_no_update BEFORE UPDATE ON release_events BEGIN SELECT RAISE(ABORT,'immutable release event'); END;
CREATE TRIGGER release_events_no_delete BEFORE DELETE ON release_events BEGIN SELECT RAISE(ABORT,'immutable release event'); END;
CREATE TRIGGER feedback_no_update BEFORE UPDATE ON feedback_quarantine BEGIN SELECT RAISE(ABORT,'immutable feedback annotation'); END;
CREATE TRIGGER feedback_no_delete BEFORE DELETE ON feedback_quarantine BEGIN SELECT RAISE(ABORT,'immutable feedback annotation'); END;
CREATE TRIGGER release_candidates_no_reinsert BEFORE INSERT ON release_candidates
WHEN EXISTS (SELECT 1 FROM release_candidates WHERE candidate_hash=NEW.candidate_hash)
BEGIN SELECT RAISE(ABORT,'immutable release candidate'); END;
CREATE TRIGGER release_approvals_no_reinsert BEFORE INSERT ON release_approvals
WHEN EXISTS (SELECT 1 FROM release_approvals WHERE approval_id=NEW.approval_id OR (agent_name=NEW.agent_name AND version=NEW.version))
BEGIN SELECT RAISE(ABORT,'immutable release approval'); END;
CREATE TRIGGER release_receipts_no_reinsert BEFORE INSERT ON release_receipts
WHEN EXISTS (SELECT 1 FROM release_receipts WHERE scope=NEW.scope AND idempotency_key=NEW.idempotency_key)
BEGIN SELECT RAISE(ABORT,'immutable release receipt'); END;
CREATE TRIGGER release_events_no_reinsert BEFORE INSERT ON release_events
WHEN EXISTS (SELECT 1 FROM release_events WHERE event_id=NEW.event_id)
BEGIN SELECT RAISE(ABORT,'immutable release event'); END;
CREATE TRIGGER feedback_no_reinsert BEFORE INSERT ON feedback_quarantine
WHEN EXISTS (SELECT 1 FROM feedback_quarantine WHERE feedback_id=NEW.feedback_id)
BEGIN SELECT RAISE(ABORT,'immutable feedback annotation'); END;
