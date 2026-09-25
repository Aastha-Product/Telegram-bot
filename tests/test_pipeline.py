"""Per-note pipeline, integration style: real triage/db/review/pipeline, fake Gemini + news + Telegram."""

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
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
SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "sample_notes.json").read_text(encoding="utf-8"))
NOTE = SAMPLES["batch_14"]
QUOTE = "the finished product pH dropped by about 0.4 units"
BODY = "Batch 14 came back with a pH drift. " * 10


class FakeBot:
    username = "testbot"

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

    def drafts(self) -> list[dict]:
        return [m for m in self.sent if m.get("reply_markup") is not None]

    def scorecards(self) -> list[str]:
        return [t for t in self.texts() if t.startswith("PUBLISHABILITY SCORECARD")]


def triage_reply(score: float = 9.0, flags: list | None = None) -> dict:
    return {
        "summary": {"core_idea": "a reorder is not always the same formula", "founder_perspective": "formulator",
                    "intended_audience": "skincare buyers", "main_insight": "check the CoA", "topic": "suppliers"},
        "parameters": [{"key": k, "score": score, "reason": "r", "evidence": QUOTE, "gap": "g", "guardrail": "pass",
                        "guardrail_note": ""} for k in triage.PARAMETER_KEYS],
        "hard_flags": flags or [],
        "category": "Industry Transparency",
        "suggested_angle": "same formula is not the same formula",
        "news_keywords": "cosmetic preservative supplier",
    }


@pytest.fixture(autouse=True)
def fresh_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db.init_db(tmp_path / "test.db")
    monkeypatch.setattr(pipeline, "_note_lock", asyncio.Lock())
    monkeypatch.setattr(pipeline, "_sweep_lock", asyncio.Lock())

    async def no_sleep(seconds: float) -> None:
        return None

    async def nothing_pending(bot) -> int:
        return 0

    monkeypatch.setattr(review.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(news.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ingest, "transcribe_pending", nothing_pending)


@pytest.fixture
def ai(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake triage model (via gemini_client) and fake drafting step (draft.draft_note)."""
    state: dict = {"triage": [], "triage_calls": 0, "draft": [], "draft_calls": []}

    async def fake_generate_json(prompt, schema, model_name, attachments=None):
        state["triage_calls"] += 1
        item = state["triage"].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_draft_note(note_text, category, angle, core_idea, news_keywords):
        state["draft_calls"].append({"note": note_text, "core_idea": core_idea, "keywords": news_keywords})
        item = state["draft"].pop(0) if state["draft"] else draft.DraftResult(
            BODY, None, [1, 4], "gemini-3.5-flash", None, {"passed": True})
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(gemini_client, "generate_json", fake_generate_json)
    monkeypatch.setattr(draft, "draft_note", fake_draft_note)
    return state


def _add(text: str = NOTE, message_id: int = 1) -> int:
    return db.add_note(message_id, -1001, text, T0)


def _process(bot: FakeBot, note_id: int) -> str:
    return asyncio.run(pipeline.process_note(bot, note_id))


# --- decision routing ------------------------------------------------------------------------


def test_qualified_note_gets_scorecard_then_draft(ai: dict) -> None:  # TEST 1
    note_id = _add()
    ai["triage"].append(triage_reply(9.0))
    bot = FakeBot()
    assert _process(bot, note_id) == "drafted"
    assert bot.texts()[0].startswith(f"Note #{note_id} · text note · QUALIFIED FOR DRAFT")
    assert "OVERALL SCORE: 9.0/10" in bot.scorecards()[0]
    [sent] = bot.drafts()
    assert BODY.strip() in sent["text"]
    assert db.get_note(note_id).status == "drafted"
    d = db.get_draft(1)
    assert d.qa == {"passed": True} and d.review_message_id == 503
    assert db.get_latest_assessment(note_id).scorecard_message_id == 502
    assert ai["draft_calls"][0]["core_idea"] == "a reorder is not always the same formula"


def test_low_score_gets_scorecard_and_no_draft(ai: dict) -> None:  # TEST 2
    note_id = _add(SAMPLES["clean_beauty"])
    ai["triage"].append(triage_reply(6.0))
    bot = FakeBot()
    assert _process(bot, note_id) == "rejected"
    assert bot.drafts() == [] and ai["draft_calls"] == []
    assert "What would make it stronger" in bot.scorecards()[0]
    assert db.get_note(note_id).status == "shelved"


def test_exactly_8_is_not_drafted(ai: dict) -> None:  # TEST 7, end to end
    note_id = _add()
    ai["triage"].append(triage_reply(8.0))
    assert _process(FakeBot(), note_id) == "rejected"
    assert ai["draft_calls"] == []


def test_guardrail_flag_overrides_high_score(ai: dict) -> None:  # TEST 5 / TEST 6
    note_id = _add()
    flag = {"type": "unsupported_claim", "detail": "sweeping industry claim", "quote": "they quietly changed"}
    ai["triage"].append(triage_reply(9.5, [flag]))
    bot = FakeBot()
    assert _process(bot, note_id) == "human_review"
    assert bot.drafts() == [] and ai["draft_calls"] == []
    assert "HUMAN REVIEW REQUIRED" in bot.scorecards()[0] and "unsupported claim" in bot.scorecards()[0]
    assert db.get_note(note_id).status == "shelved"


def test_confidential_note_is_blocked_by_code_even_if_model_misses_it(ai: dict) -> None:  # TEST 6
    cases = json.loads((Path(__file__).parent / "fixtures" / "test_cases.json").read_text(encoding="utf-8"))
    note_id = _add(cases["t6_confidential"])
    reply = triage_reply(9.0)
    for p in reply["parameters"]:
        p["evidence"] = "our supplier Aroma Chem in Vapi quoted us"
    ai["triage"].append(reply)
    assert _process(FakeBot(), note_id) == "human_review"


def test_note_is_processed_once(ai: dict) -> None:
    note_id = _add()
    ai["triage"].append(triage_reply(9.0))
    bot = FakeBot()
    _process(bot, note_id)
    assert _process(bot, note_id) == "skipped"
    assert ai["triage_calls"] == 1 and len(bot.drafts()) == 1 and len(bot.scorecards()) == 1


def test_concurrent_arrival_and_sweep_do_not_double_process(ai: dict) -> None:
    note_id = _add()
    ai["triage"].append(triage_reply(9.0))
    bot = FakeBot()

    async def both():
        return await asyncio.gather(pipeline.process_note(bot, note_id), pipeline.process_note(bot, note_id))

    assert sorted(asyncio.run(both())) == ["drafted", "skipped"]
    assert len(bot.drafts()) == 1 and len(bot.scorecards()) == 1


# --- graceful degradation ----------------------------------------------------------------------


def test_gemini_down_during_triage_notifies_and_retries_later(ai: dict) -> None:
    note_id = _add()
    ai["triage"] += [gemini_client.GeminiError("model call failed after 3 retries: ServerError 503"), triage_reply(9.0)]
    bot = FakeBot()
    assert _process(bot, note_id) == "error"
    assert bot.texts() == [pipeline.NOTICE_AI_DOWN] and "503" not in bot.texts()[0]
    assert db.get_note(note_id).status == "new" and db.get_latest_assessment(note_id) is None
    assert _process(bot, note_id) == "drafted"


def test_gemini_down_during_drafting_keeps_assessment_and_retries_drafting_only(ai: dict) -> None:
    note_id = _add()
    ai["triage"].append(triage_reply(9.0))
    ai["draft"].append(gemini_client.GeminiError("model call failed: ServerError 500"))
    bot = FakeBot()
    assert _process(bot, note_id) == "error"
    assert db.get_note(note_id).status == "new"
    assert _process(bot, note_id) == "drafted"
    assert ai["triage_calls"] == 1 and len(bot.scorecards()) == 1  # not re-scored, scorecard not re-sent


def test_unverifiable_draft_is_dropped_and_note_shelved(ai: dict) -> None:
    note_id = _add()
    ai["triage"].append(triage_reply(9.0))
    ai["draft"].append(None)
    bot = FakeBot()
    assert _process(bot, note_id) == "dropped"
    assert bot.drafts() == [] and pipeline.NOTICE_DRAFT_DROPPED in bot.texts()
    assert db.get_note(note_id).status == "shelved"


def test_telegram_down_for_scorecard_keeps_note_retryable_without_rescoring(ai: dict) -> None:
    note_id = _add(SAMPLES["clean_beauty"])
    ai["triage"].append(triage_reply(5.0))
    assert _process(FakeBot(fail_sends=20), note_id) == "error"
    assert db.get_note(note_id).status == "new"  # not shelved until Meera has seen the verdict
    bot = FakeBot()
    assert _process(bot, note_id) == "rejected"
    assert ai["triage_calls"] == 1 and len(bot.scorecards()) == 1


def test_telegram_down_for_draft_is_redelivered_by_sweep(ai: dict) -> None:
    note_id = _add()
    ai["triage"].append(triage_reply(9.0))
    bot = FakeBot()
    bot.fail_sends = 0
    # scorecard messages succeed, the draft send fails
    original = bot.send_message

    async def flaky(**kwargs):
        if kwargs.get("reply_markup") is not None and not getattr(flaky, "healed", False):
            raise NetworkError("down")
        return await original(**kwargs)

    bot.send_message = flaky
    assert _process(bot, note_id) == "drafted"
    assert [d.id for d in db.get_undelivered_drafts()] == [1]
    flaky.healed = True
    assert asyncio.run(pipeline.run_sweep(bot, "sweep-1")) in ("no_candidate", "processed")
    assert db.get_undelivered_drafts() == [] and len(bot.drafts()) == 1


def test_rss_down_still_drafts_without_news(ai: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    note_id = _add()
    ai["triage"].append(triage_reply(9.0))

    def unreachable(request):
        raise httpx.ConnectError("news.google.com unreachable")

    monkeypatch.setattr(news, "_transport", httpx.MockTransport(unreachable))
    seen_items = []

    async def real_draft_note_with_fake_model(note_text, category, angle, core_idea, news_keywords):
        seen_items.append(await news.fetch_news(news_keywords))  # the real news step, failing
        return draft.DraftResult(BODY, None, [1], "gemini-3.5-flash", None, {"passed": True})

    monkeypatch.setattr(draft, "draft_note", real_draft_note_with_fake_model)
    assert _process(FakeBot(), note_id) == "drafted"
    assert seen_items == [[]]


def test_unexpected_error_never_crashes(ai: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    note_id = _add()

    async def boom(note):
        raise KeyError("surprise")

    monkeypatch.setattr(triage, "assess_note", boom)
    bot = FakeBot()
    assert _process(bot, note_id) == "error"
    assert bot.texts() == [pipeline.NOTICE_ERROR]


# --- sweep, /run, app wiring ---------------------------------------------------------------------


def test_sweep_processes_all_pending_notes_once_per_slot(ai: dict) -> None:
    good, weak = _add(NOTE, 1), _add(SAMPLES["clean_beauty"], 2)
    ai["triage"] += [triage_reply(9.0), triage_reply(5.0)]
    bot = FakeBot()
    assert asyncio.run(pipeline.run_sweep(bot, "sweep-a")) == "drafted"
    assert db.get_note(good).status == "drafted" and db.get_note(weak).status == "shelved"
    assert asyncio.run(pipeline.run_sweep(bot, "sweep-a")) == "skipped"
    assert "drafted=1" in db.get_run("sweep-a").detail and "rejected=1" in db.get_run("sweep-a").detail


def test_sweep_transcribes_pending_voice_first(ai: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    order = []

    async def fake_transcribe_pending(bot) -> int:
        order.append("transcribe")
        return 0

    monkeypatch.setattr(ingest, "transcribe_pending", fake_transcribe_pending)
    asyncio.run(pipeline.run_sweep(FakeBot(), "sweep-b"))
    assert order == ["transcribe"]
    assert db.get_run("sweep-b").detail == "nothing pending"


def test_slot_names_use_local_time() -> None:
    now = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)  # 07:30 in Asia/Kolkata
    assert pipeline.sweep_slot(now) == "sweep-2026-09-21-0730"
    assert pipeline.sweep_slot(now, "manual") == "manual-2026-09-21-0730"


def test_retry_sweep_is_scheduled() -> None:
    application = app.build_application()
    [job] = application.job_queue.get_jobs_by_name("retry-sweep")
    assert job.job.trigger.interval.total_seconds() == config.settings.sweep_interval_minutes * 60


def test_arrival_triggers_immediate_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    started = []

    async def fake_handle(update, context):
        return 7

    async def fake_process(bot, note_id):
        return "drafted"

    monkeypatch.setattr(ingest, "handle_channel_post", fake_handle)
    monkeypatch.setattr(pipeline, "process_note", fake_process)
    application = SimpleNamespace(create_task=lambda coro, update=None: started.append(coro) or coro.close())
    asyncio.run(app.on_channel_post(SimpleNamespace(), SimpleNamespace(bot=FakeBot(), application=application)))
    assert len(started) == 1


def test_run_command_is_meera_only(ai: dict) -> None:
    _add()
    ai["triage"].append(triage_reply(9.0))

    class Msg:
        def __init__(self, user_id: int) -> None:
            self.from_user, self.replies = SimpleNamespace(id=user_id), []

        async def reply_text(self, text: str, parse_mode=None, reply_markup=None) -> None:
            self.replies.append(text)

    stranger, meera = Msg(999), Msg(config.settings.meera_user_id)
    bot = FakeBot()
    asyncio.run(pipeline.handle_run_command(SimpleNamespace(message=stranger), SimpleNamespace(bot=bot)))
    assert stranger.replies == [] and bot.sent == []
    asyncio.run(pipeline.handle_run_command(SimpleNamespace(message=meera), SimpleNamespace(bot=bot)))
    assert meera.replies == ["Processing anything pending now...", pipeline.SWEEP_REPLIES["drafted"]]


def test_threshold_is_strictly_greater_than_8_by_default() -> None:
    assert config.settings.triage_threshold == 8.0
    assert dataclasses.replace(config.settings).triage_threshold == 8.0
