"""Bounded conditional RSS/Atom polling and untrusted feed normalization."""
import codecs
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import time
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid5
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat

import httpx
from pydantic import JsonValue, ValidationError

from app.contracts.common import Nonempty, StrictModel, UtcDatetime
from app.contracts.evidence import SourceRecord
from app.contracts.scout import FeedPollReason, ScoutFeedItem, ScoutFeedPoll, ScoutRunInput
from app.scout.registry import FeedRegistry, FeedRevision, FeedSnapshot
from app.scout.run import FeedBatch, FeedItemProblem

FEED_SOURCE_NAMESPACE = UUID("d3a68c74-9530-4f7c-8c8f-6f1d2e84b912")
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_FEEDS = 20
DEFAULT_MAX_XML_DEPTH = 64
DEFAULT_MAX_XML_ELEMENTS = 50_000
MAX_EXCERPT_CHARS = 500
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


class _InvalidFeedError(ValueError):
    """Raised when an XML payload is malformed or not an RSS/Atom document."""


class _UnsupportedEncodingError(ValueError):
    """Raised when HTTP or XML charset decoding fails or is unsupported."""


class _BodyLimitError(ValueError):
    """Raised when a feed response exceeds the byte budget."""


class _CachedFeed(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    etag: str | None = None
    last_modified: str | None = None
    retrieved_at: UtcDatetime
    items: dict[Nonempty, JsonValue]
    problems: dict[Nonempty, FeedItemProblem]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    printable = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(printable.split())


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    extractor = _HTMLTextExtractor()
    extractor.feed(value)
    extractor.close()
    return _clean_text(extractor.text())


def _canonicalize_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url or len(url) > 2048 or any(not ch.isprintable() or ch.isspace() for ch in url):
        raise ValueError("invalid feed item URL")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("feed item URL must be HTTP(S) without credentials")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    port = parts.port
    default_port = 80 if scheme == "http" else 443
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    kept_query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(kept_query))
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def _parse_timestamp(raw: str | None) -> tuple[datetime | None, str | None]:
    if raw is None or not raw.strip():
        return None, "Feed item omitted publication timestamp"
    text = raw.strip()
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise ValueError("unparseable publication timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("publication timestamp must include timezone")
    return parsed.astimezone(timezone.utc), None


def _expat_to_et_tag(name: str) -> str:
    if " " not in name:
        return name
    uri, local = name.rsplit(" ", 1)
    return f"{{{uri}}}{local}"


def _parse_xml_safe(raw_bytes: bytes, charset: str | None) -> ET.Element:
    payload: bytes | str
    if charset:
        try:
            codecs.lookup(charset)
            payload = raw_bytes.decode(charset)
        except (LookupError, UnicodeDecodeError) as error:
            raise _UnsupportedEncodingError("unsupported or invalid feed charset") from error
    else:
        payload = raw_bytes

    parser = expat.ParserCreate(" ")
    builder = ET.TreeBuilder()
    depth = 0
    elements = 0

    def forbid_dtd(*_args, **_kwargs):
        raise _InvalidFeedError("DTD and entity declarations are forbidden in feed XML")

    def start_element(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, elements
        depth += 1
        elements += 1
        if depth > DEFAULT_MAX_XML_DEPTH or elements > DEFAULT_MAX_XML_ELEMENTS:
            raise _InvalidFeedError("feed XML exceeds structural limits")
        et_attrs = {_expat_to_et_tag(k): v for k, v in attrs.items()}
        builder.start(_expat_to_et_tag(name), et_attrs)

    def end_element(name: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end(_expat_to_et_tag(name))

    parser.StartDoctypeDeclHandler = forbid_dtd
    parser.EntityDeclHandler = forbid_dtd
    parser.UnparsedEntityDeclHandler = forbid_dtd
    parser.NotationDeclHandler = forbid_dtd
    parser.ExternalEntityRefHandler = forbid_dtd
    parser.SkippedEntityHandler = forbid_dtd
    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = builder.data

    try:
        parser.Parse(payload, True)
    except expat.ExpatError as error:
        unknown_code = expat.errors.codes.get(expat.errors.XML_ERROR_UNKNOWN_ENCODING)
        incorrect_code = expat.errors.codes.get(expat.errors.XML_ERROR_INCORRECT_ENCODING)
        if error.code in {unknown_code, incorrect_code} or "encoding" in str(error).lower():
            raise _UnsupportedEncodingError("unsupported XML encoding") from error
        raise _InvalidFeedError("malformed XML payload") from error

    try:
        root = builder.close()
    except ET.ParseError as error:
        raise _InvalidFeedError("incomplete XML document") from error
    if root is None:
        raise _InvalidFeedError("empty XML document")
    return root


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str | None:
    for child in element:
        if _local(child.tag) in names:
            return "".join(child.itertext())
    return None


def _atom_link(entry: ET.Element) -> str | None:
    fallback: str | None = None
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "".join(child.itertext())).strip()
        if not href:
            continue
        rel = (child.attrib.get("rel") or "alternate").strip().lower()
        if rel == "alternate":
            return href
        if fallback is None:
            fallback = href
    return fallback


def _extract_entries(root: ET.Element) -> list[dict[str, str | None]]:
    root_name = _local(root.tag)
    if root_name == "rss":
        channel = next((c for c in root if _local(c.tag) == "channel"), None)
        if channel is None:
            raise _InvalidFeedError("RSS feed missing channel element")
        nodes = [c for c in channel if _local(c.tag) == "item"]
        return [
            {
                "title": _child_text(item, "title"),
                "link": _child_text(item, "link"),
                "published": _child_text(item, "pubDate", "date", "published", "updated"),
                "summary": _child_text(item, "description", "summary", "encoded", "content"),
            }
            for item in nodes
        ]
    if root_name == "RDF":
        channel = next((c for c in root if _local(c.tag) == "channel"), None)
        if channel is None:
            raise _InvalidFeedError("RDF feed missing channel element")
        nodes = [c for c in root if _local(c.tag) == "item"] or [c for c in channel if _local(c.tag) == "item"]
        return [
            {
                "title": _child_text(item, "title"),
                "link": _child_text(item, "link"),
                "published": _child_text(item, "date", "pubDate", "published", "updated"),
                "summary": _child_text(item, "description", "summary", "encoded", "content"),
            }
            for item in nodes
        ]
    if root_name == "feed":
        nodes = [c for c in root if _local(c.tag) == "entry"]
        return [
            {
                "title": _child_text(entry, "title"),
                "link": _atom_link(entry),
                "published": _child_text(entry, "published", "updated", "date"),
                "summary": _child_text(entry, "summary", "content"),
            }
            for entry in nodes
        ]
    raise _InvalidFeedError(f"unsupported feed root element: {root_name}")


def _normalize_entries(
    entries: list[dict[str, str | None]],
    feed: FeedRevision,
    retrieved_at: datetime,
) -> tuple[dict[str, JsonValue], dict[str, FeedItemProblem]]:
    items: dict[str, JsonValue] = {}
    problems: dict[str, FeedItemProblem] = {}
    seen_urls: set[str] = set()

    for index, entry in enumerate(entries):
        title = _clean_text(entry["title"])
        raw_link = (entry["link"] or "").strip()
        if not title or len(title) > 400 or not raw_link:
            item_id = f"{feed.feed_id}:item:{index}"
            items[item_id] = {}
            problems[item_id] = "malformed_feed_item"
            continue

        try:
            canonical_url = _canonicalize_url(raw_link)
            published_at, pub_reason = _parse_timestamp(entry["published"])
        except ValueError:
            item_id = f"{feed.feed_id}:item:{index}"
            items[item_id] = {}
            problems[item_id] = "malformed_feed_item"
            continue

        digest = sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
        if canonical_url in seen_urls:
            item_id = f"{feed.feed_id}:{digest}:dup:{index}"
            items[item_id] = {}
            problems[item_id] = "duplicate_feed_item"
            continue
        seen_urls.add(canonical_url)

        locator = _clean_text(f"Feed {feed.feed_id} r{feed.revision} item {index + 1}: {title}")[:400]
        excerpt: str | None = None
        if feed.rights == "excerpt_permitted" and entry["summary"]:
            cleaned = _strip_html(entry["summary"])[:MAX_EXCERPT_CHARS].strip()
            if cleaned:
                excerpt = cleaned

        try:
            source = SourceRecord(
                source_id=uuid5(FEED_SOURCE_NAMESPACE, f"{feed.feed_id}\n{canonical_url}"),
                revision=feed.revision,
                original_url=raw_link,
                canonical_url=canonical_url,
                publisher=feed.publisher,
                source_class=feed.source_class,
                published_at=published_at,
                publication_time_reason=pub_reason,
                retrieved_at=retrieved_at,
                locator=locator,
                excerpt=excerpt,
                rights=feed.rights,
                usage_notes=feed.usage_notes,
                content_hash=None,
            )
            feed_item = ScoutFeedItem(headline=title, source=source)
        except ValidationError:
            item_id = f"{feed.feed_id}:item:{index}"
            items[item_id] = {}
            problems[item_id] = "malformed_feed_item"
            continue

        item_id = f"{feed.feed_id}:{digest}"
        items[item_id] = feed_item.model_dump(mode="json")

    return items, problems


def _read_bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    header_length = response.headers.get("Content-Length")
    if header_length is not None:
        try:
            if int(header_length) > max_bytes:
                raise _BodyLimitError("response Content-Length exceeds max_bytes")
        except ValueError:
            pass
    chunks = bytearray()
    for chunk in response.iter_bytes():
        if len(chunks) + len(chunk) > max_bytes:
            raise _BodyLimitError("streamed response exceeds max_bytes")
        chunks.extend(chunk)
    return bytes(chunks)


class RSSPoller:
    """Conditional, bounded RSS/Atom transport backed by a pinned FeedSnapshot."""

    def __init__(
        self,
        registry: FeedRegistry,
        snapshot: FeedSnapshot,
        client: httpx.Client,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_feeds: int = DEFAULT_MAX_FEEDS,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_feeds < 1:
            raise ValueError("max_feeds must be positive")
        self.registry = registry
        self.snapshot = snapshot
        self.client = client
        self.now = now
        self.clock = clock
        self.max_bytes = max_bytes
        self.max_feeds = max_feeds

    def _load_cache(self, feed: FeedRevision) -> _CachedFeed | None:
        row = self.registry.conn.execute(
            "SELECT body FROM scout_feed_cache WHERE feed_id = ? AND revision = ?",
            (feed.feed_id, feed.revision),
        ).fetchone()
        if row is None:
            return None
        try:
            return _CachedFeed.model_validate_json(row[0])
        except ValidationError:
            return None

    def _save_cache(self, feed: FeedRevision, cached: _CachedFeed) -> None:
        with self.registry.conn:
            self.registry.conn.execute(
                "INSERT INTO scout_feed_cache (feed_id, revision, body) VALUES (?, ?, ?) "
                "ON CONFLICT(feed_id, revision) DO UPDATE SET body = excluded.body",
                (feed.feed_id, feed.revision, cached.canonical_bytes().decode("utf-8")),
            )

    def _poll_one(
        self,
        feed: FeedRevision,
        *,
        timeout: float,
    ) -> tuple[ScoutFeedPoll, dict[str, JsonValue], dict[str, FeedItemProblem]]:
        cached = self._load_cache(feed)
        headers: dict[str, str] = {}
        if cached is not None:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified

        try:
            with self.client.stream("GET", feed.endpoint, headers=headers, timeout=timeout) as response:
                if response.status_code == 304:
                    if cached is None:
                        return ScoutFeedPoll(
                            feed_id=feed.feed_id, revision=feed.revision,
                            status="unavailable", reason="missing_cache",
                        ), {}, {}
                    new_etag = response.headers.get("ETag") or cached.etag
                    new_lm = response.headers.get("Last-Modified") or cached.last_modified
                    if new_etag != cached.etag or new_lm != cached.last_modified:
                        self._save_cache(feed, cached.model_copy(update={
                            "etag": new_etag, "last_modified": new_lm,
                        }))
                    return ScoutFeedPoll(
                        feed_id=feed.feed_id, revision=feed.revision,
                        status="not_modified", item_ids=tuple(cached.items),
                    ), dict(cached.items), dict(cached.problems)

                if response.status_code != 200:
                    return ScoutFeedPoll(
                        feed_id=feed.feed_id, revision=feed.revision,
                        status="unavailable", reason="http_error",
                    ), {}, {}

                raw_bytes = _read_bounded_body(response, self.max_bytes)
                charset = response.charset_encoding
                etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
        except _BodyLimitError:
            return ScoutFeedPoll(
                feed_id=feed.feed_id, revision=feed.revision,
                status="limited", reason="body_limit",
            ), {}, {}
        except httpx.TimeoutException:
            return ScoutFeedPoll(
                feed_id=feed.feed_id, revision=feed.revision,
                status="unavailable", reason="timeout",
            ), {}, {}
        except httpx.HTTPError:
            return ScoutFeedPoll(
                feed_id=feed.feed_id, revision=feed.revision,
                status="unavailable", reason="transport_error",
            ), {}, {}

        try:
            root = _parse_xml_safe(raw_bytes, charset)
            entries = _extract_entries(root)
        except _UnsupportedEncodingError:
            return ScoutFeedPoll(
                feed_id=feed.feed_id, revision=feed.revision,
                status="unavailable", reason="unsupported_encoding",
            ), {}, {}
        except _InvalidFeedError:
            return ScoutFeedPoll(
                feed_id=feed.feed_id, revision=feed.revision,
                status="unavailable", reason="invalid_rss",
            ), {}, {}

        retrieved_at = self.now()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("RSSPoller now() must return a timezone-aware datetime")
        retrieved_at = retrieved_at.astimezone(timezone.utc)

        feed_items, feed_problems = _normalize_entries(entries, feed, retrieved_at)
        self._save_cache(
            feed,
            _CachedFeed(
                etag=etag,
                last_modified=last_modified,
                retrieved_at=retrieved_at,
                items=feed_items,
                problems=feed_problems,
            ),
        )
        return ScoutFeedPoll(
            feed_id=feed.feed_id, revision=feed.revision,
            status="read", item_ids=tuple(feed_items),
        ), feed_items, feed_problems

    def __call__(self, request: ScoutRunInput, *, timeout_seconds: float) -> FeedBatch:
        if request.configuration.feed_snapshot != self.snapshot.record_hash():
            raise ValueError("feed snapshot does not match request configuration")

        deadline = self.clock() + timeout_seconds
        all_items: dict[str, JsonValue] = {}
        all_problems: dict[str, FeedItemProblem] = {}
        polls: list[ScoutFeedPoll] = []

        for index, feed in enumerate(self.snapshot.feeds):
            if index >= self.max_feeds:
                polls.append(ScoutFeedPoll(
                    feed_id=feed.feed_id, revision=feed.revision,
                    status="limited", reason="feed_limit",
                ))
                continue
            remaining = deadline - self.clock()
            if remaining <= 0:
                polls.append(ScoutFeedPoll(
                    feed_id=feed.feed_id, revision=feed.revision,
                    status="limited", reason="time_limit",
                ))
                continue

            poll, feed_items, feed_problems = self._poll_one(feed, timeout=remaining)
            polls.append(poll)
            all_items.update(feed_items)
            all_problems.update(feed_problems)

        return FeedBatch(items=all_items, polls=tuple(polls), problems=all_problems)
