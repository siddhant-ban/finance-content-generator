"""SQLite-backed curation and reproducible Scout feed configuration."""
from datetime import datetime, timezone
import sqlite3
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.contracts.common import Nonempty, StrictModel, UtcDatetime
from app.contracts.evidence import SourceUrl, Revision

FeedId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")]
SourceClass = Literal["primary", "reputable_secondary", "commentary", "unknown"]
Rights = Literal["unknown", "metadata_only", "excerpt_permitted"]
SOURCE_CLASSES = ("primary", "reputable_secondary", "commentary", "unknown")
RIGHTS = ("unknown", "metadata_only", "excerpt_permitted")


class FeedSettings(StrictModel):
    endpoint: SourceUrl
    publisher: Nonempty
    source_class: SourceClass
    rights: Rights
    usage_notes: Nonempty
    enabled: bool = True

    @model_validator(mode="after")
    def valid_endpoint(self):
        if ("#" in self.endpoint or len(self.endpoint) > 2048
                or any(not char.isprintable() or char.isspace() for char in self.endpoint)):
            raise ValueError("feed endpoint requires a URL without fragments or whitespace")
        if not self.publisher.strip() or not self.usage_notes.strip():
            raise ValueError("publisher and usage notes must be non-blank")
        return self


class FeedRevision(FeedSettings):
    feed_id: FeedId
    revision: Revision
    actor: Nonempty
    changed_at: UtcDatetime
    reason: Nonempty

    @model_validator(mode="after")
    def valid_audit_metadata(self):
        if not self.actor.strip() or not self.reason.strip():
            raise ValueError("actor and reason must be non-blank")
        return self


class FeedSnapshot(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    feeds: tuple[FeedRevision, ...]

    @model_validator(mode="after")
    def active_revisions(self):
        ids = [feed.feed_id for feed in self.feeds]
        if ids != sorted(set(ids)) or any(not feed.enabled for feed in self.feeds):
            raise ValueError("snapshot requires one active revision per feed in stable order")
        return self


BLS = "U.S. Bureau of Labor Statistics"
FED = "Board of Governors of the Federal Reserve System"
SEEDS = (
    ("bls-cpi", "https://www.bls.gov/feed/cpi.rss", BLS,
     "https://www.bls.gov/opub/copyright-information.htm"),
    ("bls-employment-situation", "https://www.bls.gov/feed/empsit.rss", BLS,
     "https://www.bls.gov/opub/copyright-information.htm"),
    ("fed-monetary-policy", "https://www.federalreserve.gov/feeds/press_monetary.xml", FED,
     "https://www.federalreserve.gov/disclaimer.htm"),
)


class FeedRegistry:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _append(self, row: FeedRevision) -> None:
        self.conn.execute("INSERT INTO scout_feed_revisions VALUES (?, ?, ?)",
                          (row.feed_id, row.revision, row.canonical_bytes().decode("utf-8")))

    def _latest(self, feed_id: str) -> FeedRevision | None:
        row = self.conn.execute(
            "SELECT body FROM scout_feed_revisions WHERE feed_id = ? ORDER BY revision DESC LIMIT 1",
            (feed_id,),
        ).fetchone()
        return FeedRevision.model_validate_json(row[0]) if row else None

    def list(self, *, history: bool = False) -> tuple[FeedRevision, ...]:
        condition = "" if history else """
            WHERE revision = (
                SELECT MAX(revision) FROM scout_feed_revisions AS h WHERE h.feed_id = r.feed_id
            )
        """
        rows = self.conn.execute(
            f"SELECT body FROM scout_feed_revisions AS r {condition} ORDER BY feed_id, revision"
        )
        return tuple(FeedRevision.model_validate_json(row[0]) for row in rows)

    def seed(self) -> tuple[FeedRevision, ...]:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for feed_id, endpoint, publisher, policy in SEEDS:
                if self._latest(feed_id) is None:
                    self._append(FeedRevision(
                        feed_id=feed_id, revision=1, endpoint=endpoint, publisher=publisher,
                        source_class="primary", rights="metadata_only",
                        usage_notes=f"Retain headline, release link and timestamps only. Publisher policy: {policy}",
                        actor="scout-seed", changed_at=datetime.now(timezone.utc), reason="Initial official feed",
                    ))
            return self.list()

    def add(self, feed_id: str, settings: FeedSettings, *, actor: str, reason: str) -> FeedRevision:
        row = FeedRevision(feed_id=feed_id, revision=1, actor=actor, reason=reason,
                           changed_at=datetime.now(timezone.utc), **settings.model_dump())
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if self._latest(feed_id) is not None:
                raise ValueError("feed ID already exists")
            self._append(row)
        return row

    def revise(self, feed_id: str, *, changes: dict[str, str | bool], actor: str, reason: str) -> FeedRevision:
        if not changes or not changes.keys() <= FeedSettings.model_fields.keys():
            raise ValueError("provide at least one feed setting to revise")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            current = self._latest(feed_id)
            if current is None:
                raise ValueError("feed ID does not exist")
            row = FeedRevision.model_validate(current.model_dump() | changes | {
                "revision": current.revision + 1, "actor": actor, "reason": reason,
                "changed_at": datetime.now(timezone.utc),
            })
            self._append(row)
        return row

    def snapshot(self, digest: str | None = None) -> FeedSnapshot:
        if digest is not None:
            row = self.conn.execute(
                "SELECT body FROM scout_feed_snapshots WHERE sha256 = ?", (digest,),
            ).fetchone()
            if row is None:
                raise ValueError("feed snapshot does not exist")
            snapshot = FeedSnapshot.model_validate_json(row[0])
            if snapshot.record_hash() != digest:
                raise ValueError("feed snapshot hash does not match stored configuration")
            return snapshot

        snapshot = FeedSnapshot(feeds=tuple(feed for feed in self.list() if feed.enabled))
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO scout_feed_snapshots VALUES (?, ?)",
                (snapshot.record_hash(), snapshot.canonical_bytes().decode("utf-8")),
            )
        return snapshot
