CREATE TABLE scout_story_revisions (
    story_id TEXT NOT NULL REFERENCES stories(story_id),
    cluster_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    run_id TEXT NOT NULL REFERENCES scout_run_requests(run_id),
    cluster_key TEXT NOT NULL CHECK(length(cluster_key) > 0),
    cluster_version TEXT NOT NULL CHECK(length(cluster_version) > 0),
    fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
    artifact_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
    reason_code TEXT NOT NULL CHECK(length(reason_code) > 0),
    created_at TEXT NOT NULL CHECK(length(created_at) > 0),
    PRIMARY KEY (story_id, revision)
) WITHOUT ROWID;

CREATE TABLE scout_cluster_members (
    story_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK(source_revision >= 1),
    source_record_hash TEXT NOT NULL CHECK(length(source_record_hash) = 64 AND source_record_hash NOT GLOB '*[^0-9a-f]*'),
    canonical_url TEXT NOT NULL CHECK(length(canonical_url) > 0),
    PRIMARY KEY (story_id, revision, source_id, source_revision),
    FOREIGN KEY (story_id, revision) REFERENCES scout_story_revisions(story_id, revision)
) WITHOUT ROWID;

CREATE INDEX idx_scout_cluster_members_source ON scout_cluster_members(source_id, canonical_url);

CREATE TRIGGER scout_story_revisions_immutable_update BEFORE UPDATE ON scout_story_revisions
BEGIN SELECT RAISE(ABORT, 'scout story revision history is immutable'); END;

CREATE TRIGGER scout_story_revisions_immutable_delete BEFORE DELETE ON scout_story_revisions
BEGIN SELECT RAISE(ABORT, 'scout story revision history is immutable'); END;

CREATE TRIGGER scout_cluster_members_immutable_update BEFORE UPDATE ON scout_cluster_members
BEGIN SELECT RAISE(ABORT, 'scout cluster membership history is immutable'); END;

CREATE TRIGGER scout_cluster_members_immutable_delete BEFORE DELETE ON scout_cluster_members
BEGIN SELECT RAISE(ABORT, 'scout cluster membership history is immutable'); END;
