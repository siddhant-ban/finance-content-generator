"""Deterministic, versioned clustering and conflict detection for News Scout."""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.contracts.scout import ScoutMember, ScoutRunInput, Warning

CLUSTER_VERSION = "scout.cluster.v1"

_SOURCE_PRIORITY = {"primary": 0, "reputable_secondary": 1, "commentary": 2, "unknown": 3}

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))

_QUARTERS = {"q1": 1, "first": 1, "q2": 2, "second": 2, "q3": 3, "third": 3, "q4": 4, "fourth": 4}

_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "its", "of", "on", "or", "over", "report", "reports", "said",
    "says", "the", "to", "us", "u", "s", "with", "year", "month", "percent", "pct",
})

_EDITORIAL_PREFIX_RE = re.compile(
    r"^(?:(?:correction|corrected|update|updated|breaking|analysis|wrap|exclusive)\s*[:\-]\s*)+",
    re.IGNORECASE,
)
_USDL_RE = re.compile(r"\b(USDL-\d{2}-\d{3,5})\b", re.IGNORECASE)
_AGENCY_FILE_RE = re.compile(
    r"\b((?:cpi|empsit|ppi|jolts|ecireg|realer|monetary|fomc|gdp|pce)[_-]?\d{6,8}[a-z]?)\b",
    re.IGNORECASE,
)
_EXPLICIT_RELEASE_RE = re.compile(
    r"\brelease[-_\s]?id[:=]\s*([a-z0-9][a-z0-9_-]{2,40})\b",
    re.IGNORECASE,
)

_DAY_MONTH_YEAR_RE = re.compile(
    rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}}),\s*(20\d{{2}})\b",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(
    rf"\b({_MONTH_PATTERN})\.?(?:\s+of)?\s+(20\d{{2}})\b",
    re.IGNORECASE,
)
_MONTH_ONLY_RE = re.compile(rf"\b({_MONTH_PATTERN})\.?\b", re.IGNORECASE)
_QUARTER_YEAR_RE = re.compile(
    r"\b(q[1-4]|first|second|third|fourth)\s*(?:quarter\s+)?(20\d{2})\b",
    re.IGNORECASE,
)
_QUARTER_ONLY_RE = re.compile(r"\b(q[1-4]|(?:first|second|third|fourth)\s+quarter)\b", re.IGNORECASE)
_BASELINE_PHRASE_RE = re.compile(
    rf"\b(?:from|vs\.?|versus|compared\s+(?:to|with)|prior\s+to)\s+(?:{_MONTH_PATTERN})\.?(?:\s+20\d{{2}})?\b",
    re.IGNORECASE,
)

_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9.])([+-]?\d+(?:\.\d+)?)\s*(?:%|percent\b|pct\b)", re.IGNORECASE)
_BPS_RE = re.compile(r"(?<![A-Za-z0-9.])([+-]?\d+(?:\.\d+)?)\s*(?:bps\b|basis\s+points?\b)", re.IGNORECASE)

_RATE_CUT_RE = re.compile(r"\b(?:cut|cuts|cutting|lower|lowers|lowered|slash|slashed|reduce|reduced)\b", re.IGNORECASE)
_RATE_HIKE_RE = re.compile(r"\b(?:hike|hikes|hiked|hiking|raise|raises|raised|tighten|tightened)\b", re.IGNORECASE)
_RATE_HOLD_RE = re.compile(r"\b(?:hold|holds|held|steady|unchanged|pause|paused|leaves?\s+rates?\s+unchanged)\b", re.IGNORECASE)

_MOVE_UP_RE = re.compile(r"\b(?:rose|rise|rises|risen|increased|increase|increases|gained|gain|climbed|jumped|surged|added)\b", re.IGNORECASE)
_MOVE_DOWN_RE = re.compile(r"\b(?:fell|fall|falls|fallen|decreased|decrease|decreases|dropped|drop|declined|shrank|lost|contracted)\b", re.IGNORECASE)

Period = tuple[str, ...]


@dataclass(frozen=True)
class _Features:
    item_id: str
    member: ScoutMember
    normalized_url: str
    release_id: str | None
    periods: frozenset[Period]
    concept: str | None
    tokens: frozenset[str]
    published_at: datetime


class _DisjointSet:
    def __init__(self, items: list[str]):
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _normalize_url(raw: str) -> str:
    parts = urlsplit(raw.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path.rstrip("/") or "/"
    query_items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query_items)), ""))


def _extract_release_id(member: ScoutMember) -> str | None:
    fields = (
        member.source.canonical_url,
        member.source.original_url,
        member.source.locator or "",
        member.headline,
        member.source.excerpt or "",
    )
    for text in fields:
        if match := _USDL_RE.search(text):
            return match.group(1).upper()
        if match := _EXPLICIT_RELEASE_RE.search(text):
            return match.group(1).lower()
        if match := _AGENCY_FILE_RE.search(text):
            return match.group(1).lower()
    return None


def _infer_month_year(month: int, published_at: datetime) -> int:
    if published_at.month == 1 and month == 12:
        return published_at.year - 1
    return published_at.year


def _periods_from_text(text: str, published_at: datetime) -> set[Period]:
    periods: set[Period] = set()
    cleaned = text
    for match in _DAY_MONTH_YEAR_RE.finditer(cleaned):
        month = _MONTHS[match.group(1).lower()]
        periods.add(("day", match.group(3), f"{month:02d}", f"{int(match.group(2)):02d}"))
    cleaned = _DAY_MONTH_YEAR_RE.sub(" ", cleaned)

    for match in _MONTH_YEAR_RE.finditer(cleaned):
        month = _MONTHS[match.group(1).lower()]
        periods.add(("month", match.group(2), f"{month:02d}"))
    cleaned = _MONTH_YEAR_RE.sub(" ", cleaned)

    for match in _QUARTER_YEAR_RE.finditer(cleaned):
        q = _QUARTERS[match.group(1).lower()]
        periods.add(("quarter", match.group(2), f"Q{q}"))
    cleaned = _QUARTER_YEAR_RE.sub(" ", cleaned)

    if not periods:
        for match in _QUARTER_ONLY_RE.finditer(cleaned):
            token = match.group(1).lower().split()[0]
            q = _QUARTERS[token]
            periods.add(("quarter", str(published_at.year), f"Q{q}"))
        for match in _MONTH_ONLY_RE.finditer(cleaned):
            month = _MONTHS[match.group(1).lower()]
            periods.add(("month", str(_infer_month_year(month, published_at)), f"{month:02d}"))
    return periods


def _extract_periods(member: ScoutMember) -> frozenset[Period]:
    assert member.source.published_at is not None
    pub = member.source.published_at
    headline_periods = _periods_from_text(member.headline, pub)
    if headline_periods:
        return frozenset(headline_periods)
    fallback = " ".join(part for part in (member.source.locator, member.source.excerpt) if part)
    if not fallback:
        return frozenset()
    stripped = _BASELINE_PHRASE_RE.sub(" ", fallback)
    return frozenset(_periods_from_text(stripped, pub))


def _concept_in_text(text: str) -> str | None:
    lowered = text.lower()
    if any(k in lowered for k in ("consumer price index", "cpi-u", "cpi-w", "consumer prices")) or re.search(r"\bcpi\b", lowered):
        return "cpi"
    if any(k in lowered for k in ("producer price index", "producer prices")) or re.search(r"\bppi\b", lowered):
        return "ppi"
    if any(k in lowered for k in ("jobless claims", "initial claims", "unemployment claims")):
        return "jobless_claims"
    if any(k in lowered for k in ("employment situation", "nonfarm payrolls", "non-farm payrolls", "jobs report", "unemployment rate")):
        return "employment_situation"
    if any(k in lowered for k in ("fomc", "federal open market committee", "federal reserve", "fed ")) and any(
        k in lowered for k in ("rate", "rates", "monetary policy", "interest", "basis points", "bps", "fomc", "policy")
    ):
        return "fed_monetary_policy"
    if any(k in lowered for k in ("personal consumption expenditures", "core pce")) or re.search(r"\bpce\b", lowered):
        return "pce"
    if "gross domestic product" in lowered or re.search(r"\bgdp\b", lowered):
        return "gdp"
    if "retail sales" in lowered:
        return "retail_sales"
    return None


def _extract_concept(member: ScoutMember) -> str | None:
    return _concept_in_text(member.headline) or _concept_in_text(member.source.excerpt or "")


def _headline_tokens(headline: str) -> frozenset[str]:
    stripped = _EDITORIAL_PREFIX_RE.sub("", headline).lower()
    words = re.findall(r"[a-z0-9]+", stripped)
    return frozenset(
        w for w in words
        if w not in _STOP_WORDS and w not in _MONTHS and not w.isdigit() and len(w) > 1
    )


def _single_period_compatible(a: Period, b: Period) -> bool:
    if a == b:
        return True
    if {a[0], b[0]} == {"day", "month"}:
        day, month = (a, b) if a[0] == "day" else (b, a)
        return day[1] == month[1] and day[2] == month[2]
    return False


def _periods_compatible(pa: frozenset[Period], pb: frozenset[Period]) -> bool:
    if not pa or not pb:
        return True
    return any(_single_period_compatible(a, b) for a in pa for b in pb)


def _period_label(period: Period) -> str:
    if period[0] == "month":
        return f"{period[1]}-{period[2]}"
    if period[0] == "quarter":
        return f"{period[1]}-{period[2]}"
    return f"{period[1]}-{period[2]}-{period[3]}"


def _extract_features(item_id: str, member: ScoutMember) -> _Features:
    assert member.source.published_at is not None
    return _Features(
        item_id=item_id,
        member=member,
        normalized_url=_normalize_url(member.source.canonical_url),
        release_id=_extract_release_id(member),
        periods=_extract_periods(member),
        concept=_extract_concept(member),
        tokens=_headline_tokens(member.headline),
        published_at=member.source.published_at,
    )


def _near_duplicate(a: _Features, b: _Features) -> bool:
    if not _periods_compatible(a.periods, b.periods):
        return False
    delta = abs(a.published_at - b.published_at)
    if a.concept is not None and a.concept == b.concept:
        if a.periods and b.periods:
            if a.concept in {"jobless_claims", "fed_monetary_policy"} and not any(
                p[0] == "day" for p in a.periods | b.periods
            ):
                return delta <= timedelta(hours=72)
            return True
        return delta <= timedelta(hours=48)
    if delta > timedelta(hours=48):
        return False
    shared = a.tokens & b.tokens
    union = a.tokens | b.tokens
    if not union or len(shared) < 3:
        return False
    return (len(shared) / len(union)) >= 0.5


def _member_sort_key(feat: _Features) -> tuple[int, datetime, str, str]:
    return (
        _SOURCE_PRIORITY[feat.member.source.source_class],
        feat.published_at,
        feat.normalized_url,
        feat.item_id,
    )


def _cluster_key(group: list[_Features]) -> str:
    concepts = {f.concept for f in group if f.concept is not None}
    periods = set().union(*(f.periods for f in group))
    if len(concepts) == 1 and len(periods) == 1:
        concept = next(iter(concepts))
        period = next(iter(periods))
        if concept not in {"jobless_claims"} or period[0] == "day":
            return f"event:{concept}:{_period_label(period)}"
    release_ids = {f.release_id for f in group if f.release_id is not None}
    if len(release_ids) == 1:
        return f"release:{next(iter(release_ids))}"
    leader = min(group, key=_member_sort_key)
    return f"url:{leader.normalized_url}"


def cluster_items(
    request: ScoutRunInput,
    items: Mapping[str, ScoutMember],
    *,
    timeout_seconds: float,
) -> Mapping[str, tuple[str, ...]]:
    """Partition eligible items using exact identifiers first, then deterministic near-duplicate rules."""
    if timeout_seconds <= 0:
        raise TimeoutError("clustering timed out")
    if not items:
        return {}
    deadline = time.monotonic() + timeout_seconds
    features = {item_id: _extract_features(item_id, member) for item_id, member in items.items()}
    ordered = sorted(features.values(), key=_member_sort_key)
    dsu = _DisjointSet([f.item_id for f in ordered])

    # Tier 1: exact fingerprints, source IDs, canonical URLs, and official release IDs.
    by_token: dict[tuple[str, str], str] = {}
    for feat in ordered:
        exact_keys: list[tuple[str, str]] = [
            ("record_hash", feat.member.ref.source_record_hash),
            ("source_id", str(feat.member.source.source_id)),
            ("url", feat.normalized_url),
        ]
        if feat.member.source.content_hash:
            exact_keys.append(("content_hash", feat.member.source.content_hash))
        if feat.release_id:
            exact_keys.append(("release_id", feat.release_id))
        for tag, value in exact_keys:
            owner = by_token.setdefault((tag, value), feat.item_id)
            dsu.union(owner, feat.item_id)

    # Tier 2: deterministic near-duplicate rules across distinct exact groups.
    for idx, left in enumerate(ordered):
        if time.monotonic() > deadline:
            raise TimeoutError("clustering timed out")
        for right in ordered[idx + 1:]:
            if dsu.find(left.item_id) == dsu.find(right.item_id):
                continue
            if _near_duplicate(left, right):
                dsu.union(left.item_id, right.item_id)

    components: dict[str, list[_Features]] = defaultdict(list)
    for feat in ordered:
        components[dsu.find(feat.item_id)].append(feat)

    result: dict[str, tuple[str, ...]] = {}
    for group in components.values():
        key = _cluster_key(group)
        if key in result:
            # Disambiguate only if two distinct components produced the same canonical key.
            leader = min(group, key=_member_sort_key)
            key = f"{key}#{leader.normalized_url}"
        result[key] = tuple(f.item_id for f in group)
    return result


def _decimal_matches(pattern: re.Pattern[str], text: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for match in pattern.finditer(text):
        try:
            values.add(Decimal(match.group(1)).normalize())
        except InvalidOperation:
            continue
    return values


def _rate_action(text: str) -> str | None:
    if _RATE_CUT_RE.search(text):
        return "cut"
    if _RATE_HIKE_RE.search(text):
        return "hike"
    if _RATE_HOLD_RE.search(text):
        return "hold"
    return None


def _movement_direction(text: str) -> str | None:
    up = bool(_MOVE_UP_RE.search(text))
    down = bool(_MOVE_DOWN_RE.search(text))
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return None


def detect_conflicting_sources(members: tuple[ScoutMember, ...]) -> bool:
    """Detect deterministic numeric or directional disagreements across dated cluster accounts."""
    if len(members) < 2:
        return False

    headline_pcts = [_decimal_matches(_PERCENT_RE, m.headline) for m in members]
    nonempty_hl = [vals for vals in headline_pcts if vals]
    if len(nonempty_hl) >= 2 and len(set().intersection(*nonempty_hl)) == 0:
        return True

    full_texts = [f"{m.headline} {m.source.excerpt or ''}" for m in members]
    full_pcts = [_decimal_matches(_PERCENT_RE, text) for text in full_texts]
    nonempty_full = [vals for vals in full_pcts if vals]
    if len(nonempty_full) >= 2 and all(len(vals) == 1 for vals in nonempty_full):
        if len({next(iter(vals)) for vals in nonempty_full}) > 1:
            return True

    bps_sets = [_decimal_matches(_BPS_RE, text) for text in full_texts]
    nonempty_bps = [vals for vals in bps_sets if vals]
    if len(nonempty_bps) >= 2 and len(set().intersection(*nonempty_bps)) == 0:
        return True

    rate_actions = {action for m in members if (action := _rate_action(m.headline)) is not None}
    if len(rate_actions) > 1:
        return True

    directions = {
        direction
        for m in members
        if "unemployment" not in m.headline.lower()
        and (direction := _movement_direction(m.headline)) is not None
    }
    if len(directions) > 1:
        return True

    return False


def combine_warnings(enrichment_warnings: tuple[Warning, ...], members: tuple[ScoutMember, ...]) -> tuple[Warning, ...]:
    """Combine model warnings with deterministic cluster-level conflict warnings."""
    combined = list(enrichment_warnings)
    if detect_conflicting_sources(members) and "conflicting_sources" not in combined:
        combined.append("conflicting_sources")
    return tuple(dict.fromkeys(combined))
