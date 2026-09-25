import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

import news

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
KEYWORDS = "cosmetic preservative supplier India"


def _item(title: str, days_ago: float, url: str, source: str = "The Hindu") -> str:
    published = format_datetime(NOW - timedelta(days=days_ago))
    return (f"<item><title>{title} - {source}</title><link>{url}</link>"
            f"<pubDate>{published}</pubDate><source url='https://x'>{source}</source></item>")


def _feed(*items: str) -> bytes:
    return (f"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>{''.join(items)}"
            "</channel></rss>").encode()


FEED = _feed(
    _item("CDSCO tightens cosmetic preservative rules for suppliers", 2, "https://news.google.com/a1"),
    _item("New cosmetic preservative supplier norms explained", 5, "https://news.google.com/a2", "Mint"),
    _item("Cosmetic preservative supplier recall from last year", 40, "https://news.google.com/old"),
    _item("Cricket: India win the series", 1, "https://news.google.com/cricket"),
    _item("Preservative in pickles", 1, "https://news.google.com/pickle"),
    _item("Cosmetic preservative supplier story, no https", 1, "http://insecure.example/x"),
    _item("CDSCO tightens cosmetic preservative rules for suppliers", 2, "https://news.google.com/a1"),
)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Serve scripted HTTP responses to news._fetch; records requests."""
    state: dict = {"responses": [], "requests": [], "sleeps": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        item = state["responses"].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_sleep(seconds: float) -> None:
        state["sleeps"].append(seconds)

    monkeypatch.setattr(news, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(news.asyncio, "sleep", fake_sleep)
    return state


def _fetch(keywords: str = KEYWORDS) -> list[news.NewsItem]:
    return asyncio.run(news.fetch_news(keywords, now=NOW))


def test_returns_recent_relevant_https_items_newest_first(served: dict) -> None:
    served["responses"].append(httpx.Response(200, content=FEED))
    items = _fetch()
    assert [i.url for i in items] == ["https://news.google.com/a1", "https://news.google.com/a2"]
    first = items[0]
    assert first.title == "CDSCO tightens cosmetic preservative rules for suppliers"  # " - The Hindu" stripped
    assert first.source == "The Hindu"
    assert first.published == NOW - timedelta(days=2)


def test_request_targets_india_edition_with_time_window(served: dict) -> None:
    served["responses"].append(httpx.Response(200, content=FEED))
    _fetch()
    url = served["requests"][0].url
    assert url.host == "news.google.com" and url.path == "/rss/search"
    assert url.params["q"] == "cosmetic preservative supplier India when:14d"
    assert (url.params["gl"], url.params["hl"], url.params["ceid"]) == ("IN", "en-IN", "IN:en")


def test_caps_number_of_items(served: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    many = _feed(*[_item(f"Cosmetic preservative supplier update {n}", n / 10, f"https://news.google.com/{n}")
                   for n in range(8)])
    served["responses"].append(httpx.Response(200, content=many))
    assert len(_fetch()) == 2


def test_nothing_relevant_returns_empty(served: dict) -> None:
    served["responses"].append(httpx.Response(200, content=_feed(_item("Cricket: India win", 1, "https://n/c"))))
    assert _fetch() == []


@pytest.mark.parametrize("keywords", ["", "   ", "the and of", "India news"])
def test_no_usable_keywords_skips_network(served: dict, keywords: str) -> None:
    assert _fetch(keywords) == []
    assert served["requests"] == []


def test_retries_server_errors_then_succeeds(served: dict) -> None:
    served["responses"] += [httpx.Response(503), httpx.ReadTimeout("slow"), httpx.Response(200, content=FEED)]
    assert len(_fetch()) == 2
    assert served["sleeps"] == [1.0, 2.0]


@pytest.mark.parametrize("failure", [
    [httpx.Response(500)] * 4,
    [httpx.ConnectError("dns")] * 4,
    [httpx.Response(404)],
    [httpx.Response(200, content=b"<html>not a feed</html>")],
    [httpx.Response(200, content=b"\x00\xff garbage")],
])
def test_any_failure_degrades_to_no_news(served: dict, failure: list, caplog) -> None:
    served["responses"] += failure
    assert _fetch() == []


def test_client_errors_are_not_retried(served: dict) -> None:
    served["responses"] += [httpx.Response(403), httpx.Response(200, content=FEED)]
    assert _fetch() == []
    assert len(served["requests"]) == 1


def test_relevance_needs_one_topical_word_in_the_headline() -> None:
    item = news.NewsItem("Preservative rules change for cosmetic makers", "S", "https://x", NOW)
    assert news.is_relevant(item, "cosmetic preservative supplier")
    assert news.is_relevant(news.NewsItem("How to know if your sunscreen really works", "S", "https://x", NOW),
                            "sunscreen formulation filter humidity")
    assert not news.is_relevant(item, "sunscreen humidity")
    assert not news.is_relevant(item, "India news")
    pickles = news.NewsItem("Preservative in pickles", "S", "https://x", NOW)
    assert not news.is_relevant(pickles, "preservative supplier cosmetics")  # one word, wrong industry


def test_recency_window() -> None:
    def at(days: float) -> news.NewsItem:
        return news.NewsItem("t", "s", "https://x", NOW - timedelta(days=days))
    assert news.is_recent(at(0), NOW) and news.is_recent(at(13.9), NOW)
    assert not news.is_recent(at(14.5), NOW)
    assert news.is_recent(at(-0.5), NOW)  # small clock skew tolerated
    assert not news.is_recent(news.NewsItem("t", "s", "https://x", None), NOW)


def test_search_goes_from_narrow_to_broad_until_something_fits(served: dict) -> None:
    served["responses"] += [httpx.Response(200, content=_feed())] * 3 + [httpx.Response(200, content=FEED)]
    items = _fetch("preservative supplier formulation audit")
    assert [r.url.params["q"] for r in served["requests"]] == [
        "preservative supplier formulation audit when:14d",
        "preservative supplier formulation when:14d",
        "preservative supplier when:14d",
        "preservative when:14d",
    ]
    assert len(items) == 2


def test_search_stops_at_the_first_query_that_finds_news(served: dict) -> None:
    served["responses"].append(httpx.Response(200, content=FEED))
    assert len(_fetch("cosmetic preservative supplier")) == 2
    assert len(served["requests"]) == 1


def test_search_queries_are_unique_and_topic_first() -> None:
    assert news.search_queries("sunscreen SPF humidity") == ["sunscreen SPF humidity", "sunscreen humidity", "sunscreen"]
    assert news.search_queries("ceramide") == ["ceramide"]
