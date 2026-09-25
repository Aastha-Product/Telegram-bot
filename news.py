"""Optional current context from Google News RSS. Must degrade to nothing on any failure.

News is enrichment, never a dependency: every failure path returns an empty list,
and relevance is decided by plain keyword overlap, not by a model.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import feedparser
import httpx

import config

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
MIN_KEYWORD_LENGTH = 4
FALLBACK_WORDS = 3
# Too generic to count as a topical match on their own.
GENERIC_WORDS = frozenset({"india", "indian", "news", "latest", "update", "brand", "brands", "market"})

# Publishers whose headlines may be cited. Google News aggregates everything (press releases,
# content farms); a hook must come from a recognisable newsroom or trade title.
CREDIBLE_SOURCES = frozenset({
    "the hindu", "hindu businessline", "businessline", "mint", "livemint", "mint lounge",
    "the economic times", "economic times", "etretail", "et healthworld", "business standard",
    "hindustan times", "the indian express", "indian express", "the times of india", "times of india",
    "financial express", "moneycontrol", "business today", "cnbc tv18", "cnbctv18", "ndtv", "ndtv profit",
    "deccan herald", "the print", "theprint", "scroll", "scroll.in", "the news minute", "forbes india",
    "fortune india", "outlook business", "yourstory", "inc42", "entrackr", "reuters", "bbc", "bbc news",
    "the guardian", "financial times", "bloomberg", "vogue india", "vogue business", "business of fashion",
    "the business of fashion", "elle india", "harper's bazaar india", "femina", "cosmetics business",
    "cosmeticsdesign-asia.com", "cosmetics design asia", "cosmeticsdesign.com", "personal care insights",
    "premium beauty news", "happi", "global cosmetic industry",
})

# Tests swap this for an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    url: str
    published: datetime | None


def build_params(keywords: str) -> dict[str, str]:
    return {
        "q": f"{keywords} when:{config.settings.news_max_age_days}d",
        "hl": "en-IN",
        "gl": "IN",
        "ceid": "IN:en",
    }


def significant_words(keywords: str) -> list[str]:
    return [w for w in keywords.lower().split() if len(w) >= MIN_KEYWORD_LENGTH and w not in GENERIC_WORDS]


def is_relevant(item: NewsItem, keywords: str) -> bool:
    """A headline must share at least two topical words with the note (or one, if that's all we have)."""
    words = significant_words(keywords)
    if not words:
        return False
    title = item.title.lower()
    hits = sum(1 for w in words if w in title)
    return hits >= min(2, len(words))


def is_credible(item: NewsItem) -> bool:
    return item.source.strip().lower() in CREDIBLE_SOURCES


def to_record(item: NewsItem, relevance: str) -> dict[str, str]:
    """What gets stored and shown to Meera for a cited news hook."""
    return {
        "headline": item.title,
        "source": item.source,
        "date": item.published.strftime("%d %b %Y") if item.published else "unknown",
        "url": item.url,
        "relevance": relevance,
    }


def is_recent(item: NewsItem, now: datetime) -> bool:
    if item.published is None:
        return False
    age = now - item.published
    # A day of future slack absorbs publisher clock/timezone skew.
    return -timedelta(days=1) <= age <= timedelta(days=config.settings.news_max_age_days)


def parse_items(xml: bytes) -> list[NewsItem]:
    feed = feedparser.parse(xml)
    items = []
    for entry in feed.entries:
        url = str(entry.get("link", ""))
        title = str(entry.get("title", "")).strip()
        source = str(entry.get("source", {}).get("title", "")).strip()
        if not url.startswith("https://") or not title:
            continue
        # Google News appends " - Publisher" to titles; keep the headline only.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        published = None
        if entry.get("published_parsed"):
            published = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=UTC)
        items.append(NewsItem(title=title, source=source or "unknown", url=url, published=published))
    return items


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def _fetch(params: dict[str, str]) -> bytes:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True,
                                 transport=_transport) as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.get(GOOGLE_NEWS_RSS, params=params)
                response.raise_for_status()
                return response.content
            except Exception as exc:
                if not _retryable(exc) or attempt == MAX_RETRIES:
                    raise
                delay = BACKOFF_BASE_SECONDS * 2**attempt
                log.warning("news.retry attempt=%d error=%s wait_s=%.1f", attempt + 1, type(exc).__name__, delay)
                await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _search(keywords: str, now: datetime) -> tuple[int, list[NewsItem]]:
    items = parse_items(await _fetch(build_params(keywords)))
    return len(items), [i for i in items if is_recent(i, now) and is_credible(i) and is_relevant(i, keywords)]


async def fetch_news(keywords: str, now: datetime | None = None) -> list[NewsItem]:
    """Recent, credible, relevant headlines for these keywords; [] on any failure or if nothing fits."""
    keywords = keywords.strip()
    words = significant_words(keywords)
    if not words:
        return []
    now = now or datetime.now(UTC)
    try:
        fetched, kept = await _search(keywords, now)
        # Google News ANDs every term, so long queries often match nothing; retry broader once.
        if not kept and len(words) > FALLBACK_WORDS:
            fetched, kept = await _search(" ".join(words[:FALLBACK_WORDS]), now)
    except Exception as exc:
        log.warning("news.unavailable error=%s", type(exc).__name__)
        return []
    # Newest first, one per URL.
    seen: set[str] = set()
    unique = []
    for item in sorted(kept, key=lambda i: i.published, reverse=True):
        if item.url not in seen:
            seen.add(item.url)
            unique.append(item)
    result = unique[: config.settings.news_max_items]
    log.info("news.fetched fetched=%d relevant=%d returned=%d", fetched, len(kept), len(result))
    return result
