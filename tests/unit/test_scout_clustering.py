"""Unit tests for deterministic, versioned Scout clustering and conflict detection."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from app.contracts.evidence import SourceRecord
from app.contracts.scout import ScoutFeedItem, ScoutMember, ScoutRunInput, source_ref
from app.scout.cluster import CLUSTER_VERSION, cluster_items, detect_conflicting_sources
from app.scout.run import Clusterer


def _request(cluster_version: str = CLUSTER_VERSION) -> ScoutRunInput:
    return ScoutRunInput.model_validate_json(json.dumps({
        "schema_version": "1.0",
        "run_id": str(uuid4()),
        "invocation_id": str(uuid4()),
        "window_start": "2025-09-01T00:00:00Z",
        "window_end": "2025-12-01T00:00:00Z",
        "topic_policy": {"version": "v1", "topics": ["prices", "jobs", "borrowing"]},
        "fingerprints": {"version": "v1", "known": []},
        "configuration": {
            "version": "v1",
            "feed_snapshot": "0" * 64,
            "cluster_version": cluster_version,
            "prompt_version": "v1",
            "prompt_hash": "1" * 64,
            "provider": "fake",
            "model": "recorded-v1",
            "temperature": 0.0,
        },
        "release_id": str(uuid4()),
        "limits": {"max_items": 50, "max_tokens": 1000, "timeout_seconds": 10},
    }))


def _member(
    *,
    headline: str,
    canonical_url: str,
    publisher: str = "U.S. Bureau of Labor Statistics",
    source_class: str = "primary",
    published_at: datetime | None = None,
    excerpt: str | None = None,
    locator: str = "Opening paragraph",
    revision: int = 1,
    source_id=None,
    content_hash: str | None = None,
) -> ScoutMember:
    pub = published_at or datetime(2025, 9, 11, 12, 30, tzinfo=timezone.utc)
    source = SourceRecord(
        source_id=source_id or uuid4(),
        revision=revision,
        original_url=canonical_url,
        canonical_url=canonical_url,
        publisher=publisher,
        source_class=source_class,
        published_at=pub,
        publication_time_reason=None,
        retrieved_at=pub + timedelta(minutes=5),
        locator=locator,
        excerpt=excerpt,
        rights="excerpt_permitted" if excerpt else "metadata_only",
        usage_notes="Permitted test source.",
        content_hash=content_hash,
    )
    item = ScoutFeedItem(headline=headline, source=source)
    return ScoutMember(headline=item.headline, source=item.source, ref=source_ref(item.source))


def test_cluster_version_and_protocol_compatibility():
    assert CLUSTER_VERSION == "scout.cluster.v1"
    clusterer: Clusterer = cluster_items
    assert clusterer(_request(), {}, timeout_seconds=5.0) == {}


def test_exact_matches_group_first_by_content_hash_canonical_url_and_release_id():
    req = _request()
    shared_hash = "a" * 64
    items = {
        "hash-1": _member(
            headline="Official CPI report released",
            canonical_url="https://primary.example.com/report-a",
            content_hash=shared_hash,
        ),
        "hash-2": _member(
            headline="Syndicated mirror of CPI report",
            canonical_url="https://mirror.example.com/copy-b",
            source_class="reputable_secondary",
            publisher="Wire Mirror",
            content_hash=shared_hash,
        ),
        "url-1": _member(
            headline="Treasury auction draws strong demand",
            canonical_url="https://Markets.Example.com/auctions/10yr/",
        ),
        "url-2": _member(
            headline="Updated: Treasury auction draws strong demand",
            canonical_url="https://markets.example.com/auctions/10yr",
        ),
        "rel-1": _member(
            headline="Producer Price Index release summary",
            canonical_url="https://www.bls.gov/news.release/archives/ppi_09102025.htm",
            locator="Release USDL-25-1320 Table A",
        ),
        "rel-2": _member(
            headline="Wholesale inflation edges higher",
            canonical_url="https://news.example.com/wholesale-inflation",
            source_class="reputable_secondary",
            publisher="Financial Wire",
            excerpt="Data from BLS release USDL-25-1320 showed producer prices rose.",
        ),
    }

    groups = cluster_items(req, items, timeout_seconds=5.0)
    clustered_sets = {frozenset(ids) for ids in groups.values()}
    assert clustered_sets == {
        frozenset({"hash-1", "hash-2"}),
        frozenset({"url-1", "url-2"}),
        frozenset({"rel-1", "rel-2"}),
    }


def test_overlapping_primary_and_secondary_accounts_same_period_cluster_together():
    req = _request()
    pub = datetime(2025, 9, 11, 12, 30, tzinfo=timezone.utc)
    items = {
        "bls-aug": _member(
            headline="Consumer Price Index - August 2025",
            canonical_url="https://www.bls.gov/news.release/archives/cpi_09112025.htm",
            published_at=pub,
            excerpt="The all-items CPI-U rose 0.4 percent in August on a seasonally adjusted basis.",
        ),
        "sec-aug": _member(
            headline="U.S. consumer prices rose 0.4% in August 2025 as shelter costs climbed",
            canonical_url="https://reuters.example.com/markets/us-cpi-august-2025",
            publisher="Reuters",
            source_class="reputable_secondary",
            published_at=pub + timedelta(minutes=35),
            excerpt="Consumer prices increased 0.4 percent in August 2025, matching expectations.",
        ),
    }

    groups = cluster_items(req, items, timeout_seconds=5.0)
    assert len(groups) == 1
    (members,) = groups.values()
    assert set(members) == {"bls-aug", "sec-aug"}

    reversed_groups = cluster_items(req, dict(reversed(list(items.items()))), timeout_seconds=5.0)
    assert list(reversed_groups.keys()) == list(groups.keys())


def test_different_release_periods_produce_separate_cluster_keys():
    req = _request()
    items = {
        "sep-cpi": _member(
            headline="Consumer Price Index - September 2025",
            canonical_url="https://www.bls.gov/news.release/archives/cpi_10152025.htm",
            published_at=datetime(2025, 10, 15, 12, 30, tzinfo=timezone.utc),
            excerpt="The all-items CPI-U rose 0.3 percent in September 2025.",
        ),
        "oct-cpi": _member(
            headline="Consumer Price Index - October 2025",
            canonical_url="https://www.bls.gov/news.release/archives/cpi_11132025.htm",
            published_at=datetime(2025, 11, 13, 13, 30, tzinfo=timezone.utc),
            excerpt="The all-items CPI-U rose 0.2 percent in October 2025.",
        ),
        "sep-cpi-sec": _member(
            headline="U.S. CPI rose 0.3% in September 2025",
            canonical_url="https://news.example.com/us-cpi-september-2025",
            publisher="Market Wire",
            source_class="reputable_secondary",
            published_at=datetime(2025, 10, 15, 13, 0, tzinfo=timezone.utc),
        ),
    }

    groups = cluster_items(req, items, timeout_seconds=5.0)
    assert len(groups) == 2
    assert {frozenset(ids) for ids in groups.values()} == {
        frozenset({"sep-cpi", "sep-cpi-sec"}),
        frozenset({"oct-cpi"}),
    }


def test_correction_headline_preserves_stable_cluster_key():
    req = _request()
    sid = uuid4()
    pub = datetime(2025, 9, 11, 12, 30, tzinfo=timezone.utc)
    rev1 = _member(
        source_id=sid,
        revision=1,
        headline="Consumer Price Index - August 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_09112025.htm",
        published_at=pub,
        excerpt="The all-items CPI-U rose 0.4 percent in August.",
    )
    rev2 = _member(
        source_id=sid,
        revision=2,
        headline="Correction: Consumer Price Index - August 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_09112025.htm",
        published_at=pub,
        excerpt="Corrected: The all-items CPI-U rose 0.3 percent in August.",
    )

    k1 = next(iter(cluster_items(req, {"bls": rev1}, timeout_seconds=5.0)))
    k2 = next(iter(cluster_items(req, {"bls": rev2}, timeout_seconds=5.0)))
    assert k1 == k2


def test_deterministic_conflicting_sources_detection():
    pub = datetime(2025, 9, 11, 12, 30, tzinfo=timezone.utc)
    agree_a = _member(
        headline="Consumer Price Index - August 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_09112025.htm",
        published_at=pub,
        excerpt="The all-items CPI-U rose 0.4 percent in August.",
    )
    agree_b = _member(
        headline="U.S. consumer prices rose 0.4% in August 2025",
        canonical_url="https://reuters.example.com/aug-cpi",
        publisher="Reuters",
        source_class="reputable_secondary",
        published_at=pub + timedelta(minutes=20),
        excerpt="Consumer prices increased 0.4% in August.",
    )
    agree_hl_a = _member(
        headline="CPI rose 0.4% in August 2025",
        canonical_url="https://www.bls.gov/news.release/archives/cpi_09112025.htm",
        published_at=pub,
    )
    assert detect_conflicting_sources((agree_a, agree_b)) is False
    assert detect_conflicting_sources((agree_hl_a, agree_b)) is False

    bps_agree_a = _member(
        headline="Federal Reserve cuts interest rates by 25 bps",
        canonical_url="https://federalreserve.example.com/fomc-cut",
        published_at=pub,
    )
    bps_agree_b = _member(
        headline="Fed lowers benchmark rate by 25 basis points",
        canonical_url="https://news.example.com/fomc-cut",
        publisher="News Wire",
        source_class="reputable_secondary",
        published_at=pub + timedelta(minutes=5),
    )
    assert detect_conflicting_sources((bps_agree_a, bps_agree_b)) is False

    bps_disagree = _member(
        headline="Fed lowers benchmark rate by 50 basis points",
        canonical_url="https://other.example.com/fomc-cut",
        publisher="Other Wire",
        source_class="reputable_secondary",
        published_at=pub + timedelta(minutes=6),
    )
    assert detect_conflicting_sources((bps_agree_a, bps_disagree)) is True

    disagree_b = _member(
        headline="U.S. consumer prices rose 0.2% in August 2025",
        canonical_url="https://outlet.example.com/aug-cpi",
        publisher="Outlet B",
        source_class="reputable_secondary",
        published_at=pub + timedelta(minutes=25),
        excerpt="Consumer prices increased 0.2 percent in August.",
    )
    assert detect_conflicting_sources((agree_a, disagree_b)) is True

    fed_hold = _member(
        headline="Federal Reserve holds benchmark interest rate steady",
        canonical_url="https://federalreserve.example.com/fomc-1",
        published_at=pub,
    )
    fed_cut = _member(
        headline="Federal Reserve cuts interest rates by 25 bps",
        canonical_url="https://news.example.com/fomc-cut",
        publisher="News Wire",
        source_class="reputable_secondary",
        published_at=pub + timedelta(minutes=10),
    )
    assert detect_conflicting_sources((fed_hold, fed_cut)) is True


def test_quarters_and_general_headline_time_window_separation():
    req = _request()
    pub = datetime(2025, 10, 30, 12, 30, tzinfo=timezone.utc)
    items = {
        "q2-gdp": _member(
            headline="Gross Domestic Product, Q2 2025",
            canonical_url="https://bea.example.gov/gdp-q2-2025",
            published_at=pub,
        ),
        "q3-gdp": _member(
            headline="Gross Domestic Product, Q3 2025",
            canonical_url="https://bea.example.gov/gdp-q3-2025",
            published_at=pub,
        ),
        "bond-1": _member(
            headline="Treasury yields climb following 10-year auction",
            canonical_url="https://markets.example.com/bond-auction-a",
            published_at=pub,
        ),
        "bond-2": _member(
            headline="10-year Treasury yields climb after bond auction",
            canonical_url="https://news.example.com/bond-auction-b",
            publisher="Market Wire",
            source_class="reputable_secondary",
            published_at=pub + timedelta(hours=2),
        ),
        "bond-later": _member(
            headline="Treasury yields climb following 10-year auction",
            canonical_url="https://markets.example.com/bond-auction-next-month",
            published_at=pub + timedelta(days=10),
        ),
    }

    groups = cluster_items(req, items, timeout_seconds=5.0)
    assert {frozenset(ids) for ids in groups.values()} == {
        frozenset({"q2-gdp"}),
        frozenset({"q3-gdp"}),
        frozenset({"bond-1", "bond-2"}),
        frozenset({"bond-later"}),
    }

