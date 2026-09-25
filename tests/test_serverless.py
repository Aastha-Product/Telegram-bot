"""Vercel mode: ASGI routes, cron auth, in-request processing, config rules, processing claims."""

import asyncio
import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import app
import config
import db
import pipeline

SECRET = "s3cret_webhook_token_value_1234"
CRON = "cron_secret_value_0987654321"
UPDATE = {"update_id": 42, "channel_post": {"message_id": 7, "date": 1790000000,
                                           "chat": {"id": config.settings.telegram_chat_id, "type": "channel"},
                                           "text": "a note"}}
T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def vercel_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    monkeypatch.setattr(config, "settings", dataclasses.replace(
        config.settings, public_url="https://bot.vercel.app", webhook_secret=SECRET, cron_secret=CRON,
        serverless=True))
    db.init_db(tmp_path / "test.db")
    calls: dict = {"prepared": 0, "processed": [], "swept": []}

    def fake_prepare() -> None:
        calls["prepared"] += 1

    async def fake_process(body: bytes) -> None:
        calls["processed"].append(json.loads(body)["update_id"])

    monkeypatch.setattr(app, "_prepare_serverless", fake_prepare)
    monkeypatch.setattr(app, "_process_serverless_update", fake_process)
    return calls


def call(method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None,
         chunks: int = 1) -> tuple[int, str]:
    """Drive the ASGI app like Vercel does and return (status, body text)."""
    scope = {"type": "http", "method": method, "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    size = max(1, len(body) // chunks + 1)
    parts = [body[i:i + size] for i in range(0, len(body), size)] or [b""]
    messages = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1} for i, p in enumerate(parts)]
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app.app(scope, receive, send))
    return sent[0]["status"], sent[1]["body"].decode()


# --- routes ------------------------------------------------------------------------------------


def test_healthz_and_unknown_paths() -> None:
    assert call("GET", "/healthz") == (200, "ok")
    assert call("GET", "/")[0] == 404
    assert call("POST", "/telegram/guessed")[0] == 404
    assert call("GET", f"/telegram/{SECRET}")[0] == 404


def test_webhook_processes_authenticated_update_before_replying(vercel_settings: dict) -> None:
    status, _ = call("POST", f"/telegram/{SECRET}", json.dumps(UPDATE).encode(),
                     {app.WEBHOOK_SECRET_HEADER: SECRET}, chunks=3)
    assert status == 200
    assert vercel_settings["processed"] == [42] and vercel_settings["prepared"] == 1


@pytest.mark.parametrize("header", [None, "wrong", SECRET[:-1]])
def test_webhook_rejects_bad_secret_without_touching_telegram(vercel_settings: dict, header) -> None:
    headers = {app.WEBHOOK_SECRET_HEADER: header} if header else {}
    assert call("POST", f"/telegram/{SECRET}", json.dumps(UPDATE).encode(), headers)[0] == 403
    assert vercel_settings["processed"] == [] and vercel_settings["prepared"] == 0


def test_webhook_rejects_malformed_json(vercel_settings: dict) -> None:
    assert call("POST", f"/telegram/{SECRET}", b"not json", {app.WEBHOOK_SECRET_HEADER: SECRET})[0] == 400
    assert vercel_settings["processed"] == []


def test_oversized_body_is_refused(vercel_settings: dict) -> None:
    big = b"x" * (app.MAX_WEBHOOK_BODY_BYTES + 10)
    assert call("POST", f"/telegram/{SECRET}", big, {app.WEBHOOK_SECRET_HEADER: SECRET}, chunks=4)[0] == 413


def test_handler_crash_returns_500_not_a_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(body: bytes) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(app, "_process_serverless_update", boom)
    assert call("POST", f"/telegram/{SECRET}", json.dumps(UPDATE).encode(), {app.WEBHOOK_SECRET_HEADER: SECRET})[0] == 500


# --- cron ------------------------------------------------------------------------------------------


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def test_cron_requires_the_bearer_secret(vercel_settings: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sweep(bot, slot: str) -> str:
        vercel_settings["swept"].append(slot)
        return "no_candidate"

    monkeypatch.setattr(app, "Bot", FakeBot)
    monkeypatch.setattr(pipeline, "run_sweep", fake_sweep)
    assert call("GET", "/cron/sweep")[0] == 401
    assert call("GET", "/cron/sweep", headers={"Authorization": "Bearer nope"})[0] == 401
    assert call("GET", "/cron/sweep", headers={"Authorization": CRON})[0] == 401  # missing "Bearer "
    assert vercel_settings["swept"] == []
    assert call("GET", "/cron/sweep", headers={"Authorization": f"Bearer {CRON}"}) == (200, "no_candidate")
    assert vercel_settings["swept"][0].startswith("sweep-")


def test_cron_disabled_when_no_secret_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, cron_secret=None))
    assert call("GET", "/cron/sweep", headers={"Authorization": "Bearer "})[0] == 401


def test_lifespan_is_acknowledged() -> None:
    messages = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app.app({"type": "lifespan"}, receive, send))
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# --- in-request processing -------------------------------------------------------------------------


def test_serverless_processes_notes_inside_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    done, scheduled = [], []

    async def fake_process(bot, note_id: int) -> str:
        done.append(note_id)
        return "rejected"

    monkeypatch.setattr(pipeline, "process_note", fake_process)
    context = SimpleNamespace(bot=None, application=SimpleNamespace(create_task=lambda *a, **k: scheduled.append(a)))
    asyncio.run(app._process_now_or_later(SimpleNamespace(), context, 5))
    assert done == [5] and scheduled == []


def test_serverless_build_has_no_scheduler() -> None:
    application = app.build_application(serverless=True)
    with pytest.warns(Warning, match="JobQueue"):  # PTB warns on reading a job_queue that isn't there
        assert application.job_queue is None


# --- config rules for Vercel ---------------------------------------------------------------------


BASE_ENV = {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "-1001", "TELEGRAM_REVIEW_CHAT_ID": "5",
            "MEERA_USER_ID": "5", "GEMINI_API_KEY": "k"}


def test_vercel_requires_database_webhook_secret_and_cron_secret() -> None:
    with pytest.raises(config.ConfigError) as exc:
        config.load_settings({**BASE_ENV, "VERCEL": "1"})
    message = str(exc.value)
    assert "DATABASE_URL" in message and "WEBHOOK_SECRET" in message and "CRON_SECRET" in message
    ok = config.load_settings({**BASE_ENV, "VERCEL": "1", "DATABASE_URL": "postgresql://u:p@h:6543/postgres",
                               "WEBHOOK_SECRET": SECRET, "CRON_SECRET": CRON})
    assert ok.serverless and ok.database_url and "u:p@" not in repr(ok) and CRON not in repr(ok)


def test_database_url_must_be_postgres() -> None:
    with pytest.raises(config.ConfigError, match="DATABASE_URL"):
        config.load_settings({**BASE_ENV, "DATABASE_URL": "mysql://nope"})


def test_local_mode_needs_none_of_the_vercel_settings() -> None:
    s = config.load_settings(BASE_ENV)
    assert not s.serverless and s.database_url is None


# --- processing claims (serverless copies must not double-process) ---------------------------------


def test_note_can_only_be_claimed_by_one_worker() -> None:
    note_id = db.add_note(1, -1001, "a note long enough to process", T0)
    assert db.claim_note(note_id, 360) is True
    assert db.claim_note(note_id, 360) is False  # a webhook retry or the cron arrives meanwhile
    db.release_note(note_id)
    assert db.claim_note(note_id, 360) is True


def test_expired_claim_can_be_retaken_after_a_crash() -> None:
    note_id = db.add_note(1, -1001, "a note long enough to process", T0)
    assert db.claim_note(note_id, -1) is True  # lease already expired, as if the worker died
    assert db.claim_note(note_id, 360) is True


def test_only_new_notes_can_be_claimed() -> None:
    note_id = db.add_note(1, -1001, "a note long enough to process", T0)
    db.set_note_status(note_id, "shelved")
    assert db.claim_note(note_id, 360) is False


def test_pipeline_skips_a_note_claimed_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "_note_lock", asyncio.Lock())
    note_id = db.add_note(1, -1001, "a note long enough to process", T0)
    db.claim_note(note_id, 360)
    assert asyncio.run(pipeline.process_note(SimpleNamespace(), note_id)) == "skipped"


# --- Postgres placeholder translation ----------------------------------------------------------------


def test_postgres_connection_translates_placeholders() -> None:
    seen = []

    class FakePg:
        def execute(self, sql, params):
            seen.append((sql, params))
            return "cursor"

    conn = db._PostgresConnection(FakePg())
    assert conn.execute("SELECT * FROM notes WHERE id = ? AND status = ?", (1, "new")) == "cursor"
    assert seen == [("SELECT * FROM notes WHERE id = %s AND status = %s", (1, "new"))]


def test_no_query_contains_a_literal_percent_sign() -> None:
    source = Path(db.__file__).read_text(encoding="utf-8")
    assert "%" not in source.replace("%s", "")  # psycopg would misread a bare %


def test_vercel_json_declares_function_limits_and_daily_cron() -> None:
    spec = json.loads((Path(app.__file__).parent / "vercel.json").read_text(encoding="utf-8"))
    assert spec["functions"]["app.py"]["maxDuration"] == 300
    [cron] = spec["crons"]
    assert cron["path"] == "/cron/sweep"
    minute, hour, *rest = cron["schedule"].split()
    assert minute.isdigit() and hour.isdigit() and rest == ["*", "*", "*"]  # daily: Hobby allows no more
