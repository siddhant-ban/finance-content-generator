CREATE TABLE artifacts (
    sha256 TEXT PRIMARY KEY CHECK(length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    media_type TEXT NOT NULL CHECK(length(media_type) > 0),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    schema_version TEXT NOT NULL CHECK(schema_version = '1.0')
) WITHOUT ROWID;
CREATE TRIGGER artifacts_immutable_update BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifact metadata is immutable'); END;
CREATE TRIGGER artifacts_immutable_delete BEFORE DELETE ON artifacts
BEGIN SELECT RAISE(ABORT, 'artifact metadata is immutable'); END;
CREATE TRIGGER artifacts_immutable_reinsert BEFORE INSERT ON artifacts
WHEN EXISTS (SELECT 1 FROM artifacts WHERE sha256 = NEW.sha256)
BEGIN SELECT RAISE(ABORT, 'artifact metadata is immutable'); END;
