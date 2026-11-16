CREATE TABLE human_decisions (
 decision_id TEXT PRIMARY KEY, story_id TEXT NOT NULL REFERENCES stories(story_id),
 payload TEXT NOT NULL CHECK(json_valid(payload))
) WITHOUT ROWID;
CREATE TABLE review_receipts (
 scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 decision_id TEXT NOT NULL REFERENCES human_decisions(decision_id),
 PRIMARY KEY(scope,idempotency_key)
) WITHOUT ROWID;
CREATE TRIGGER human_decisions_no_update BEFORE UPDATE ON human_decisions
BEGIN SELECT RAISE(ABORT,'immutable human decision'); END;
CREATE TRIGGER human_decisions_no_delete BEFORE DELETE ON human_decisions
BEGIN SELECT RAISE(ABORT,'immutable human decision'); END;
CREATE TRIGGER human_decisions_no_replace BEFORE INSERT ON human_decisions
WHEN EXISTS(SELECT 1 FROM human_decisions WHERE decision_id=NEW.decision_id)
BEGIN SELECT RAISE(ABORT,'immutable human decision'); END;
CREATE TRIGGER review_receipts_no_update BEFORE UPDATE ON review_receipts
BEGIN SELECT RAISE(ABORT,'immutable review receipt'); END;
CREATE TRIGGER review_receipts_no_delete BEFORE DELETE ON review_receipts
BEGIN SELECT RAISE(ABORT,'immutable review receipt'); END;
CREATE TRIGGER review_receipts_no_replace BEFORE INSERT ON review_receipts
WHEN EXISTS(SELECT 1 FROM review_receipts WHERE scope=NEW.scope AND idempotency_key=NEW.idempotency_key)
BEGIN SELECT RAISE(ABORT,'immutable review receipt'); END;
CREATE TABLE release_reviewers (
 approval_id TEXT PRIMARY KEY REFERENCES release_approvals(approval_id),
 reviewer_provider TEXT NOT NULL, reviewer_id INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER release_reviewers_no_update BEFORE UPDATE ON release_reviewers
BEGIN SELECT RAISE(ABORT,'immutable reviewer audit'); END;
CREATE TRIGGER release_reviewers_no_delete BEFORE DELETE ON release_reviewers
BEGIN SELECT RAISE(ABORT,'immutable reviewer audit'); END;
CREATE TRIGGER release_reviewers_no_replace BEFORE INSERT ON release_reviewers
WHEN EXISTS(SELECT 1 FROM release_reviewers WHERE approval_id=NEW.approval_id)
BEGIN SELECT RAISE(ABORT,'immutable reviewer audit'); END;
