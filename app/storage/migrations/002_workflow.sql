CREATE TABLE stories (
 story_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>=1), payload TEXT NOT NULL CHECK(json_valid(payload)),
 manifest_hash TEXT REFERENCES artifacts(sha256), approval_id TEXT,
 resume_target TEXT
) WITHOUT ROWID;
CREATE TABLE approval_decisions (
 decision_id TEXT PRIMARY KEY, story_id TEXT NOT NULL REFERENCES stories(story_id),
 manifest_hash TEXT NOT NULL REFERENCES artifacts(sha256), story_version INTEGER NOT NULL,
 principal_id TEXT NOT NULL CHECK(length(principal_id)>0)
) WITHOUT ROWID;
CREATE TABLE state_events (
 event_id TEXT PRIMARY KEY, story_id TEXT NOT NULL REFERENCES stories(story_id),
 version INTEGER NOT NULL, reason_code TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)),
 UNIQUE(story_id,version)
) WITHOUT ROWID;
CREATE TABLE state_receipts (
 caller_scope TEXT NOT NULL, operation TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, response TEXT NOT NULL CHECK(json_valid(response)),
 PRIMARY KEY(caller_scope,operation,idempotency_key)
) WITHOUT ROWID;
CREATE TRIGGER stories_initial BEFORE INSERT ON stories
WHEN NEW.state!='DISCOVERED' OR NEW.version!=1 OR NEW.approval_id IS NOT NULL
BEGIN SELECT RAISE(ABORT,'story must start discovered'); END;
CREATE TRIGGER stories_no_replace BEFORE INSERT ON stories
WHEN EXISTS (SELECT 1 FROM stories WHERE story_id=NEW.story_id)
BEGIN SELECT RAISE(ABORT,'story replacement forbidden'); END;
CREATE TRIGGER stories_no_delete BEFORE DELETE ON stories
BEGIN SELECT RAISE(ABORT,'story deletion forbidden'); END;
CREATE TRIGGER stories_edges BEFORE UPDATE ON stories
WHEN NEW.story_id!=OLD.story_id OR NEW.run_id!=OLD.run_id OR NEW.version!=OLD.version+1 OR NOT (
    (OLD.state='DISCOVERED' AND NEW.state='CLUSTERED') OR
    (OLD.state='CLUSTERED' AND NEW.state='SCORED') OR
    (OLD.state='SCORED' AND NEW.state='RESEARCHED') OR
    (OLD.state='RESEARCHED' AND NEW.state='VERIFIED') OR
    (OLD.state='VERIFIED' AND NEW.state='SCRIPTED') OR
    (OLD.state='SCRIPTED' AND NEW.state='RENDERED') OR
    (OLD.state='RENDERED' AND NEW.state='AWAITING_APPROVAL') OR
    (OLD.state='DISCOVERED' AND NEW.state='REJECTED') OR
    (OLD.state='DISCOVERED' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='DISCOVERED' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='CLUSTERED' AND NEW.state='REJECTED') OR
    (OLD.state='CLUSTERED' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='CLUSTERED' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='SCORED' AND NEW.state='REJECTED') OR
    (OLD.state='SCORED' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='SCORED' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='RESEARCHED' AND NEW.state='REJECTED') OR
    (OLD.state='RESEARCHED' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='RESEARCHED' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='VERIFIED' AND NEW.state='REJECTED') OR
    (OLD.state='VERIFIED' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='VERIFIED' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='SCRIPTED' AND NEW.state='REJECTED') OR
    (OLD.state='SCRIPTED' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='SCRIPTED' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='RENDERED' AND NEW.state='REJECTED') OR
    (OLD.state='RENDERED' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='RENDERED' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='AWAITING_APPROVAL' AND NEW.state='REJECTED') OR
    (OLD.state='AWAITING_APPROVAL' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='AWAITING_APPROVAL' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='APPROVED_FOR_PUBLISH' AND NEW.state='REJECTED') OR
    (OLD.state='APPROVED_FOR_PUBLISH' AND NEW.state='NEEDS_RESEARCH') OR
    (OLD.state='APPROVED_FOR_PUBLISH' AND NEW.state='NEEDS_REVISION') OR
    (OLD.state='NEEDS_RESEARCH' AND NEW.state='REJECTED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='REJECTED') OR
    (OLD.state='NEEDS_RESEARCH' AND NEW.state='DISCOVERED') OR
    (OLD.state='NEEDS_RESEARCH' AND NEW.state='CLUSTERED') OR
    (OLD.state='NEEDS_RESEARCH' AND NEW.state='SCORED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='DISCOVERED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='CLUSTERED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='SCORED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='RESEARCHED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='VERIFIED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='SCRIPTED') OR
    (OLD.state='NEEDS_REVISION' AND NEW.state='RENDERED') OR
    (OLD.state='AWAITING_APPROVAL' AND NEW.state='AWAITING_APPROVAL') OR
    (OLD.state='APPROVED_FOR_PUBLISH' AND NEW.state='AWAITING_APPROVAL') OR
    (OLD.state='AWAITING_APPROVAL' AND NEW.state='APPROVED_FOR_PUBLISH'))
BEGIN SELECT RAISE(ABORT,'forbidden state edge or version'); END;
CREATE TRIGGER stories_resume BEFORE UPDATE ON stories
WHEN OLD.state IN ('NEEDS_RESEARCH','NEEDS_REVISION') AND NEW.state!='REJECTED'
 AND (OLD.resume_target IS NULL OR NEW.state!=OLD.resume_target)
BEGIN SELECT RAISE(ABORT,'saved checkpoint required'); END;
CREATE TRIGGER stories_approval BEFORE UPDATE ON stories
WHEN NEW.state='APPROVED_FOR_PUBLISH' AND NOT EXISTS (
 SELECT 1 FROM approval_decisions d WHERE d.decision_id=NEW.approval_id
 AND d.story_id=OLD.story_id AND d.story_version=OLD.version
 AND d.manifest_hash=OLD.manifest_hash AND NEW.manifest_hash=OLD.manifest_hash)
BEGIN SELECT RAISE(ABORT,'matching current approval decision required'); END;
CREATE TRIGGER stories_revoke BEFORE UPDATE ON stories
WHEN NEW.state!='APPROVED_FOR_PUBLISH' AND NEW.approval_id IS NOT NULL
BEGIN SELECT RAISE(ABORT,'approval must be revoked'); END;
CREATE TRIGGER state_events_immutable_update BEFORE UPDATE ON state_events
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER state_events_immutable_delete BEFORE DELETE ON state_events
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER state_events_immutable_reinsert BEFORE INSERT ON state_events
WHEN EXISTS (SELECT 1 FROM state_events WHERE event_id=NEW.event_id OR (story_id=NEW.story_id AND version=NEW.version))
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER state_receipts_immutable_update BEFORE UPDATE ON state_receipts
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER state_receipts_immutable_delete BEFORE DELETE ON state_receipts
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER state_receipts_immutable_reinsert BEFORE INSERT ON state_receipts
WHEN EXISTS (SELECT 1 FROM state_receipts WHERE caller_scope=NEW.caller_scope AND operation=NEW.operation AND idempotency_key=NEW.idempotency_key)
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER approval_decisions_immutable_update BEFORE UPDATE ON approval_decisions
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER approval_decisions_immutable_delete BEFORE DELETE ON approval_decisions
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
CREATE TRIGGER approval_decisions_immutable_reinsert BEFORE INSERT ON approval_decisions
WHEN EXISTS (SELECT 1 FROM approval_decisions WHERE decision_id=NEW.decision_id)
BEGIN SELECT RAISE(ABORT,'immutable record'); END;
