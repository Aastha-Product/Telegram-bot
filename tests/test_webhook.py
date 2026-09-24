import asyncio
import dataclasses
import json
import logging
import socket

import httpx
import pytest
import tornado.httpserver

import app
import config

SECRET = "s3cret_webhook_token_value_1234"
UPDATE = {"update_id": 42, "channel_post": {"message_id": 7, "date": 1790000000,
                                           "chat": {"id": config.settings.telegram_chat_id, "type": "channel"},
                                           "text": "a note"}}


@pytest.fixture(autouse=True)
def webhook_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, public_url="https://bot.example.com", webhook_secret=SECRET))


# --- request validation -----------------------------------------------------------------


def test_valid_request_yields_update() -> None:
    status, update = app.parse_webhook(SECRET, json.dumps(UPDATE).encode(), None)
    assert status == 200 and update.update_id == 42 and update.channel_post.text == "a note"


@pytest.mark.parametrize("header", [None, "", "wrong", SECRET + "x", SECRET[:-1]])
def test_wrong_or_missing_secret_is_forbidden(header: str | None) -> None:
    assert app.parse_webhook(header, json.dumps(UPDATE).encode(), None) == (403, None)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"', b"{}", b'{"channel_post": 5}'])
def test_malformed_body_is_rejected(body: bytes) -> None:
    assert app.parse_webhook(SECRET, body, None)[0] == 400


def test_no_secret_configured_rejects_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, webhook_secret=None))
    assert app.parse_webhook("", json.dumps(UPDATE).encode(), None) == (403, None)


def test_webhook_secret_is_redacted_from_logs() -> None:
    record = logging.LogRecord("tornado.access", logging.WARNING, __file__, 1,
                               "403 POST /telegram/%s (1.2.3.4)", (SECRET,), None)
    assert app.RedactSecrets([SECRET]).filter(record)
    assert SECRET not in record.getMessage()


# --- real HTTP against the tornado app ----------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve_and_call(calls):
    queue: asyncio.Queue = asyncio.Queue()
    fake_application = type("FakeApp", (), {"bot": None, "update_queue": queue})()
    server = tornado.httpserver.HTTPServer(app.make_web_app(fake_application),
                                           max_body_size=app.MAX_WEBHOOK_BODY_BYTES)
    port = _free_port()
    server.listen(port, address="127.0.0.1")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            results = []
            for call in calls:
                try:
                    results.append(await call(client))
                except httpx.TransportError:
                    results.append(None)  # server refused the request outright (e.g. body too large)
    finally:
        server.stop()
    queued = []
    while not queue.empty():
        queued.append(queue.get_nowait())
    return results, queued


def test_http_routes_end_to_end() -> None:
    body = json.dumps(UPDATE)
    calls = [
        lambda c: c.get("/healthz"),
        lambda c: c.post(f"/telegram/{SECRET}", content=body, headers={app.WEBHOOK_SECRET_HEADER: SECRET}),
        lambda c: c.post(f"/telegram/{SECRET}", content=body, headers={app.WEBHOOK_SECRET_HEADER: "nope"}),
        lambda c: c.post(f"/telegram/{SECRET}", content=body),
        lambda c: c.post("/telegram/guessed-path", content=body, headers={app.WEBHOOK_SECRET_HEADER: SECRET}),
        lambda c: c.get(f"/telegram/{SECRET}"),
        lambda c: c.post(f"/telegram/{SECRET}", content=b"x" * (app.MAX_WEBHOOK_BODY_BYTES + 10),
                         headers={app.WEBHOOK_SECRET_HEADER: SECRET}),
    ]
    results, queued = asyncio.run(_serve_and_call(calls))
    health, ok, wrong_secret, no_secret, wrong_path, get_method, oversized = results
    assert (health.status_code, health.text) == (200, "ok")
    assert ok.status_code == 200
    assert wrong_secret.status_code == 403 and no_secret.status_code == 403
    assert wrong_path.status_code == 404
    assert get_method.status_code == 405
    assert oversized is None or oversized.status_code >= 400
    assert [u.update_id for u in queued] == [42]  # only the authenticated request reached the bot
