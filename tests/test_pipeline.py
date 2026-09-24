import asyncio
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import NetworkError

import app
import config
import db
import draft
import gemini_client
import ingest
import news
import pipeline
import review
import triage

T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
GOOD = "Batch 14 came back with a pH drift. " * 10


class FakeBot:
    def __init__(self, fail_sends: int = 0) -> None:
        self.fail_sends = fail_sends
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> SimpleNamespace:
        if self.fail_sends:
            self.fail_sends -= 1
            raise NetworkError("telegram down")
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=500 + len(self.sent))

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def drafts_sent(self) -> list[dict]:
        return [m for m in self.sent if m.get("reply_markup") is not None]


@pytest.fixture(autouse=True)
def fresh_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db.init_db(tmp_path / "test.db")
    monkeypatch.setattr(pipeline, "_run_lock", asyncio.Lock())
    monkeypatch.setattr(review.asyncio, "sleep", _no_sleep)

    async def no_pending(bot) -> int:
        return 0

    monkeypatch.setattr(ingest, "transcribe_pending", no_pending)


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub the model-facing stages; each test tweaks the dict to simulate outcomes."""
    state = {"scores": {}, "triage_error": None, "news": [], "draft": GOOD, "draft_calls": []}

    async def fake_score_notes(notes):
        if state["triage_error"]:
            raise state["triage_error"]
        results = []
        for n in notes:
            score = state["scores"].get(n.content, 0.0)
            results.append(triage.TriageResult(n.id, score, score >= 6, "Industry Transparency", "r",
                                               "angle", "cosmetic preservative supplier"))
        return results

    async def fake_fetch_news(keywords):
        return state["news"]

    async def fake_make_draft(note_text, category, angle, exemplars, items):
        state["draft_calls"].append({"note": note_text, "news": items, "exemplars": exemplars})
        body = state["draft"]
        if isinstance(body, Exception):
            raise body
        return None if body is None else draft.DraftResult(body, None, [p.id for p in exemplars], "gemini-3.5-flash")

    monkeypatch.setattr(triage, "score_notes", fake_score_notes)
    monkeypatch.setattr(news, "fetch_news", fake_fetch_news)
    monkeypatch.setattr(draft, "make_draft", fake_make_draft)
    return state


def _note(text: str, message_id: int, score: float, stages: dict) -> int:
    stages["scores"][text] = score
    return db.add_note(message_id, -1001, text, T0)


def _run(bot: FakeBot, slot: str = "2026-09-21-0730") -> str:
    return asyncio.run(pipeline.run_pipeline(bot, slot))


# --- happy path & idempotency --------------------------------------------------------


def test_drafts_the_best_note_and_sends_it_for_review(stages: dict) -> None:
    low = _note("low value note", 1, 3.0, stages)
    best = _note("batch fourteen note", 2, 9.0, stages)
    bot = FakeBot()
    assert _run(bot) == "drafted"
    [sent] = bot.drafts_sent()
    assert GOOD.strip() in sent["text"]
    assert db.get_note(best).status == "drafted" and db.get_note(low).status == "new"
    run = db.get_run("2026-09-21-0730")
    assert run.outcome == "drafted" and f"note {best}" in run.detail
    assert stages["draft_calls"][0]["note"] == "batch fourteen note"


def test_same_slot_never_drafts_twice(stages: dict) -> None:
    _note("batch fourteen note", 1, 9.0, stages)
    _note("cold pressed note", 2, 8.0, stages)
    bot = FakeBot()
    assert _run(bot) == "drafted"
    assert _run(bot) == "skipped"  # e.g. scheduler double-fire after a redeploy
    assert len(bot.drafts_sent()) == 1
    assert _run(bot, "2026-09-23-0730") == "drafted"  # next slot takes the next note
    assert len(bot.drafts_sent()) == 2


def test_overlapping_runs_are_serialised(stages: dict) -> None:
    _note("batch fourteen note", 1, 9.0, stages)
    bot = FakeBot()

    async def both():
        return await asyncio.gather(pipeline.run_pipeline(bot, "manual-a"), pipeline.run_pipeline(bot, "sched-b"))

    outcomes = asyncio.run(both())
    assert sorted(outcomes) == ["drafted", "no_candidate"]
    assert len(bot.drafts_sent()) == 1


# --- graceful degradation --------------------------------------------------------------


def test_no_candidate_notice_once_per_dry_spell(stages: dict) -> None:
    _note("thin note", 1, 2.0, stages)
    bot = FakeBot()
    assert _run(bot, "s1") == "no_candidate"
    assert _run(bot, "s2") == "no_candidate"
    assert bot.texts() == [pipeline.NOTICE_NOTHING_READY]
    assert bot.drafts_sent() == []


def test_gemini_down_skips_run_quietly_and_retries_next_slot(stages: dict) -> None:
    note_id = _note("batch fourteen note", 1, 9.0, stages)
    stages["triage_error"] = gemini_client.GeminiError("model call failed after 3 retries: ServerError 503 UNAVAILABLE")
    bot = FakeBot()
    assert _run(bot, "s1") == "error"
    assert bot.texts() == [pipeline.NOTICE_AI_DOWN]
    assert "503" not in bot.texts()[0]
    assert db.get_note(note_id).status == "new"
    assert "gemini unavailable" in db.get_run("s1").detail

    stages["triage_error"] = None
    assert _run(bot, "s2") == "drafted"


def test_gemini_down_during_drafting(stages: dict) -> None:
    note_id = _note("batch fourteen note", 1, 9.0, stages)
    stages["draft"] = gemini_client.GeminiError("model call failed: ServerError 500 INTERNAL")
    bot = FakeBot()
    assert _run(bot) == "error"
    assert bot.texts() == [pipeline.NOTICE_AI_DOWN]
    assert db.get_note(note_id).status == "new"  # untouched, so it's retried


def test_rss_down_still_drafts_without_news(stages: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _note("batch fourteen note", 1, 9.0, stages)
    monkeypatch.undo()  # restore the real news.fetch_news...
    monkeypatch.setattr(pipeline, "_run_lock", asyncio.Lock())
    monkeypatch.setattr(review.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(news.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(ingest, "transcribe_pending", _zero)

    def unreachable(request):
        raise httpx.ConnectError("news.google.com unreachable")

    monkeypatch.setattr(news, "_transport", httpx.MockTransport(unreachable))  # ...but make RSS fail
    monkeypatch.setattr(triage, "score_notes", _score_all_high)
    calls = []

    async def fake_make_draft(note_text, category, angle, exemplars, items):
        calls.append(items)
        return draft.DraftResult(GOOD, None, [1], "gemini-3.5-flash")

    monkeypatch.setattr(draft, "make_draft", fake_make_draft)
    bot = FakeBot()
    assert _run(bot) == "drafted"
    assert calls == [[]]  # drafted with no news, not aborted


async def _zero(bot) -> int:
    return 0


async def _score_all_high(notes):
    return [triage.TriageResult(n.id, 9.0, True, "Industry Transparency", "r", "a", "cosmetic preservative supplier")
            for n in notes]


def test_unverifiable_draft_is_dropped_and_note_shelved(stages: dict) -> None:
    first = _note("batch fourteen note", 1, 9.0, stages)
    second = _note("cold pressed note", 2, 8.0, stages)
    stages["draft"] = None
    bot = FakeBot()
    assert _run(bot, "s1") == "error"
    assert bot.drafts_sent() == [] and bot.texts() == [pipeline.NOTICE_DRAFT_DROPPED]
    assert db.get_note(first).status == "shelved"

    stages["draft"] = GOOD
    assert _run(bot, "s2") == "drafted"
    assert db.get_note(second).status == "drafted"


def test_telegram_down_keeps_draft_and_redelivers_next_slot(stages: dict) -> None:
    _note("batch fourteen note", 1, 9.0, stages)
    _note("cold pressed note", 2, 8.0, stages)
    bot = FakeBot(fail_sends=10)
    assert _run(bot, "s1") == "drafted"
    assert "delivery pending" in db.get_run("s1").detail
    [undelivered] = db.get_undelivered_drafts()

    bot.fail_sends = 0
    assert _run(bot, "s2") == "drafted"
    assert db.get_run("s2").detail == f"redelivered draft {undelivered.id}"
    assert len(bot.drafts_sent()) == 1  # redelivered, and no second draft in the same slot
    assert len(stages["draft_calls"]) == 1


def test_unexpected_error_never_crashes(stages: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _note("batch fourteen note", 1, 9.0, stages)

    async def boom(notes):
        raise KeyError("surprise")

    monkeypatch.setattr(triage, "score_notes", boom)
    bot = FakeBot()
    assert _run(bot) == "error"
    assert bot.texts() == [pipeline.NOTICE_ERROR]
    assert db.get_run("2026-09-21-0730").outcome == "error"


def test_pending_voice_notes_are_transcribed_first(stages: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    async def fake_transcribe_pending(bot) -> int:
        called.append(bot)
        return 0

    monkeypatch.setattr(ingest, "transcribe_pending", fake_transcribe_pending)
    bot = FakeBot()
    _run(bot)
    assert called == [bot]


# --- slots, schedule & /run ---------------------------------------------------------------


def test_slots_use_local_time() -> None:
    now = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)  # 07:30 in Asia/Kolkata
    assert pipeline.scheduled_slot(now) == "2026-09-21-0730"
    assert pipeline.manual_slot(now) == "manual-2026-09-21-0730"


def test_job_scheduled_mon_wed_fri_at_0730_ist() -> None:
    application = app.build_application()
    [job] = application.job_queue.get_jobs_by_name("draft-pipeline")
    trigger = job.job.trigger
    assert str(trigger.timezone) == "Asia/Kolkata"
    fields = {f.name: str(f) for f in trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("7", "30")
    assert fields["day_of_week"] == "mon,wed,fri"
    assert config.settings.schedule_time == time(7, 30)


def test_run_command_is_meera_only(stages: dict) -> None:
    _note("batch fourteen note", 1, 9.0, stages)

    class Msg:
        def __init__(self, user_id: int) -> None:
            self.from_user, self.replies = SimpleNamespace(id=user_id), []

        async def reply_text(self, text: str, parse_mode=None) -> None:
            self.replies.append(text)

    stranger, meera = Msg(999), Msg(config.settings.meera_user_id)
    bot = FakeBot()
    asyncio.run(pipeline.handle_run_command(SimpleNamespace(message=stranger), SimpleNamespace(bot=bot)))
    assert stranger.replies == [] and bot.sent == []
    asyncio.run(pipeline.handle_run_command(SimpleNamespace(message=meera), SimpleNamespace(bot=bot)))
    assert meera.replies == ["Running a draft now...", pipeline.RUN_REPLIES["drafted"]]
    assert len(bot.drafts_sent()) == 1
