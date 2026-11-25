-- Curator changes are appended. Snapshots contain the complete active revision set.
CREATE TABLE scout_feed_revisions (
    feed_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    body TEXT NOT NULL CHECK (json_valid(body)),
    PRIMARY KEY (feed_id, revision)
) WITHOUT ROWID;

CREATE TRIGGER scout_feed_revisions_no_update BEFORE UPDATE ON scout_feed_revisions
BEGIN SELECT RAISE(ABORT, 'feed revisions are append-only'); END;
CREATE TRIGGER scout_feed_revisions_no_delete BEFORE DELETE ON scout_feed_revisions
BEGIN SELECT RAISE(ABORT, 'feed revisions are append-only'); END;

CREATE TABLE scout_feed_snapshots (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    body TEXT NOT NULL CHECK (json_valid(body))
) WITHOUT ROWID;

CREATE TRIGGER scout_feed_snapshots_no_update BEFORE UPDATE ON scout_feed_snapshots
BEGIN SELECT RAISE(ABORT, 'feed snapshots are immutable'); END;
CREATE TRIGGER scout_feed_snapshots_no_delete BEFORE DELETE ON scout_feed_snapshots
BEGIN SELECT RAISE(ABORT, 'feed snapshots are immutable'); END;

CREATE TABLE scout_feed_cache (
    feed_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    PRIMARY KEY (feed_id, revision),
    FOREIGN KEY (feed_id, revision) REFERENCES scout_feed_revisions(feed_id, revision)
) WITHOUT ROWID;
