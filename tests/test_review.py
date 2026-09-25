import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User
from telegram.error import BadRequest, NetworkError, RetryAfter

import app
import config
import db
import draft
import gemini_client
import review

MEERA = config.settings.meera_user_id
REVIEW_CHAT = config.settings.telegram_review_chat_id
STRANGER = 999_999
T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
BODY = ("Batch 14 came back and the pH had drifted by 0.4 units. <That> & more. " * 5).strip()
REWRITE = "My own version of the post. " * 10


class FakeBot:
    """Records sends; `failures` is a list of exceptions raised by successive send attempts."""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> SimpleNamespace:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1000 + len(self.sent))


class FakeMessage:
    def __init__(self, text: str | None = None, user_id: int = MEERA, chat_id: int = REVIEW_CHAT) -> None:
        self.text, self.is_accessible, self.message_id = text, True, 77
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=chat_id)
        self.replies: list[tuple[str, str | None]] = []
        self.markups: list = []
        self.markup_removed = False

    async def reply_text(self, text: str, parse_mode: str | None = None, reply_markup=None) -> None:
        self.replies.append((text, parse_mode))
        self.markups.append(reply_markup)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.markup_removed = reply_markup is None


class FakeQuery:
    def __init__(self, data: str, user_id: int = MEERA) -> None:
        self.data, self.from_user = data, SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(review.asyncio, "sleep", fake_sleep)
    return slept


def _pending_draft(body: str = BODY, source_url: str | None = None) -> db.Draft:
    note_id = db.add_note(1, -1001, "batch fourteen came back and the pH had drifted by 0.4 units", T0)
    db.set_note_triage(note_id, 8.5, True, "Industry Transparency", "r", "a", "k")
    return db.get_draft(db.add_draft(note_id, body, "gemini-3.5-flash", [4, 12], source_url))


def _press(data: str, user_id: int = MEERA) -> FakeQuery:
    query = FakeQuery(data, user_id)
    asyncio.run(review.handle_callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=FakeBot())))
    return query


def _say(text: str, bot: FakeBot | None = None, user_id: int = MEERA, chat_id: int = REVIEW_CHAT) -> FakeMessage:
    message = FakeMessage(text, user_id, chat_id)
    asyncio.run(review.handle_review_message(SimpleNamespace(message=message), SimpleNamespace(bot=bot or FakeBot())))
    return message


# --- delivery ----------------------------------------------------------------------


def test_send_for_review_delivers_with_buttons_and_records_message() -> None:
    d = _pending_draft(source_url="https://news.google.com/rss/articles/x")
    bot = FakeBot()
    assert asyncio.run(review.send_for_review(bot, d.id)) is True
    [sent] = bot.sent
    assert sent["chat_id"] == REVIEW_CHAT
    rows = [[b.callback_data for b in row] for row in sent["reply_markup"].inline_keyboard]
    assert rows == [[f"approve:{d.id}", f"edit:{d.id}"], [f"reject:{d.id}", f"regen:{d.id}"]]
    assert sent["text"].startswith(f"LINKEDIN DRAFT · Draft #{d.id} · rev 1 · Industry Transparency · score 8.5/10")
    assert BODY in sent["text"]
    assert "News source used: https://news.google.com/rss/articles/x" in sent["text"]
    assert "Nothing is posted anywhere unless you post it yourself." in sent["text"]
    assert db.get_draft(d.id).review_message_id == 1001
    assert db.get_undelivered_drafts() == []


def test_failed_delivery_leaves_draft_undelivered_for_next_run(caplog) -> None:
    d = _pending_draft()
    bot = FakeBot([NetworkError("down")] * 4)
    assert asyncio.run(review.send_for_review(bot, d.id)) is False
    assert [x.id for x in db.get_undelivered_drafts()] == [d.id]
    assert "review.delivery_failed" in caplog.text


def test_delivery_retries_and_honours_retry_after(no_sleep: list[float]) -> None:
    d = _pending_draft()
    bot = FakeBot([RetryAfter(7), NetworkError("blip")])
    assert asyncio.run(review.send_for_review(bot, d.id)) is True
    assert no_sleep == [7.0, 2.0]


def test_bad_request_is_not_retried() -> None:
    d = _pending_draft()
    bot = FakeBot([BadRequest("chat not found")])
    assert asyncio.run(review.send_for_review(bot, d.id)) is False
    assert bot.failures == [] and bot.sent == []


def test_only_pending_drafts_are_sent() -> None:
    d = _pending_draft()
    db.set_draft_status(d.id, "discarded")
    bot = FakeBot()
    assert asyncio.run(review.send_for_review(bot, d.id)) is False
    assert asyncio.run(review.send_for_review(bot, 9999)) is False
    assert bot.sent == []


def test_approved_text_is_escaped_monospace_or_plain_when_too_long() -> None:
    text, mode = review.format_approved("a < b & c")
    assert mode == "HTML" and "<pre>a &lt; b &amp; c</pre>" in text
    text, mode = review.format_approved("&" * 3000)
    assert mode is None and text.endswith("&" * 3000)


def test_notice_never_raises() -> None:
    asyncio.run(review.send_notice(FakeBot([BadRequest("nope")]), "hello"))


# --- buttons ------------------------------------------------------------------------


def test_stranger_cannot_press_buttons() -> None:
    d = _pending_draft()
    for action in ("approve", "discard", "edit"):
        query = _press(f"{action}:{d.id}", user_id=STRANGER)
        assert query.answers == [(review.NOT_AUTHORISED, True)]
    after = db.get_draft(d.id)
    assert after.status == "pending_review" and not after.awaiting_edit


def test_approve_returns_copyable_text_once() -> None:
    d = _pending_draft()
    query = _press(f"approve:{d.id}")
    assert db.get_draft(d.id).status == "approved"
    assert query.answers == [("Approved", False)]
    assert query.message.markup_removed
    [(text, mode)] = query.message.replies
    assert mode == "HTML" and text.startswith("Approved. Copy the text below and post it on LinkedIn yourself.")
    assert "&lt;That&gt; &amp; more" in text

    again = _press(f"approve:{d.id}")  # double tap
    assert again.answers == [(review.ALREADY_HANDLED, False)] and again.message.replies == []


def test_discard_shelves_the_note() -> None:
    d = _pending_draft()
    query = _press(f"discard:{d.id}")
    assert db.get_draft(d.id).status == "discarded"
    assert db.get_note(d.note_id).status == "shelved"
    assert "Nothing was posted" in query.message.replies[0][0]
    assert _press(f"approve:{d.id}").answers == [(review.ALREADY_HANDLED, False)]


def test_edit_press_waits_for_meera_and_survives_restart(fresh_db: Path) -> None:
    d = _pending_draft()
    query = _press(f"edit:{d.id}")
    assert "full rewrite" in query.message.replies[0][0]
    db.init_db(fresh_db)  # simulate a process restart
    assert db.get_awaiting_edit().id == d.id


@pytest.mark.parametrize("data", ["approve:", "publish:1", "approve:1;drop", "approve:1:2", ""])
def test_malformed_callback_data_is_rejected(data: str) -> None:
    _pending_draft()
    assert _press(data).answers == [("Unknown action.", False)]
    assert db.get_draft(1).status == "pending_review"


# --- edit replies ------------------------------------------------------------------------


def test_reply_without_pending_edit_is_explained() -> None:
    _pending_draft()
    message = _say("make it shorter")
    assert "No draft is waiting for an edit" in message.replies[0][0]


def test_messages_from_others_or_other_chats_are_ignored() -> None:
    d = _pending_draft()
    _press(f"edit:{d.id}")
    assert _say(REWRITE, user_id=STRANGER).replies == []
    assert _say(REWRITE, chat_id=-100555).replies == []
    assert db.get_draft(d.id).status == "pending_review"


def test_full_rewrite_is_kept_verbatim_as_new_revision() -> None:
    d = _pending_draft()
    _press(f"edit:{d.id}")
    bot = FakeBot()
    _say(REWRITE, bot)
    old = db.get_draft(d.id)
    new = db.get_draft(d.id + 1)
    assert old.status == "superseded" and not old.awaiting_edit
    assert (new.revision, new.status, new.model) == (2, "pending_review", "meera-edit")
    assert new.body == REWRITE.strip()
    assert new.exemplar_ids == [4, 12]
    assert bot.sent and bot.sent[0]["text"].startswith(f"LINKEDIN DRAFT · Draft #{new.id} · rev 2") and "your edit" in bot.sent[0]["text"]


def test_overlong_rewrite_is_refused_without_changes() -> None:
    d = _pending_draft()
    _press(f"edit:{d.id}")
    message = _say("x" * (config.settings.draft_max_chars + 1))
    assert "trim" in message.replies[0][0]
    assert db.get_draft(d.id).status == "pending_review" and db.get_awaiting_edit().id == d.id


def test_short_instruction_triggers_one_validated_redraft(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake_revise(note_text: str, previous: str, instruction: str) -> tuple[str, dict]:
        seen.update(note=note_text, previous=previous, instruction=instruction)
        return "Revised body. " * 20, {"passed": True}

    monkeypatch.setattr(draft, "revise_draft", fake_revise)
    d = _pending_draft()
    _press(f"edit:{d.id}")
    bot = FakeBot()
    _say("shorter, open with the pH", bot)
    assert seen == {"note": db.get_note(d.note_id).content, "previous": BODY, "instruction": "shorter, open with the pH"}
    new = db.get_draft(d.id + 1)
    assert new.model == config.settings.draft_model and new.revision == 2
    assert db.get_draft(d.id).status == "superseded"
    assert len(bot.sent) == 1


def test_failed_redraft_keeps_original_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_revise(*args) -> None:
        return None

    monkeypatch.setattr(draft, "revise_draft", fake_revise)
    d = _pending_draft()
    _press(f"edit:{d.id}")
    message = _say("add a statistic")
    assert "couldn't produce a redraft" in message.replies[-1][0]
    assert db.get_draft(d.id).status == "pending_review"
    assert db.get_awaiting_edit() is None


def test_ai_outage_during_redraft_keeps_edit_open(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_revise(*args) -> None:
        raise gemini_client.GeminiError("model call failed: ServerError 503 UNAVAILABLE")

    monkeypatch.setattr(draft, "revise_draft", fake_revise)
    d = _pending_draft()
    _press(f"edit:{d.id}")
    message = _say("shorter")
    assert "couldn't reach the AI service" in message.replies[-1][0]
    assert "503" not in message.replies[-1][0]  # no error details to Telegram
    assert db.get_awaiting_edit().id == d.id


def test_start_greets_meera_only() -> None:
    mine, theirs = FakeMessage("/start"), FakeMessage("/start", user_id=STRANGER)
    asyncio.run(review.handle_start(SimpleNamespace(message=mine), SimpleNamespace(bot=FakeBot())))
    asyncio.run(review.handle_start(SimpleNamespace(message=theirs), SimpleNamespace(bot=FakeBot())))
    assert "listening" in mine.replies[0][0] and theirs.replies == []


def test_start_delivers_drafts_that_were_waiting_for_it() -> None:
    d = _pending_draft()  # never delivered: Meera hadn't pressed Start yet
    bot = FakeBot()
    asyncio.run(review.handle_start(SimpleNamespace(message=FakeMessage("/start")), SimpleNamespace(bot=bot)))
    assert [m["text"].split(" · ")[1] for m in bot.sent] == [f"Draft #{d.id}"]
    assert db.get_undelivered_drafts() == []


# --- startup self-check ------------------------------------------------------------------


class StartupBot(FakeBot):
    username = "Meeravoicebot"

    def __init__(self, reachable: bool = True) -> None:
        super().__init__()
        self.reachable = reachable

    async def send_chat_action(self, chat_id: int, action: str) -> bool:
        if not self.reachable:
            raise BadRequest("PEER_ID_INVALID")
        return True


def test_startup_delivers_waiting_drafts_when_chat_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import ingest

    async def none_pending(bot) -> int:
        return 0

    monkeypatch.setattr(ingest, "transcribe_pending", none_pending)
    _pending_draft()
    bot = StartupBot()
    asyncio.run(app.on_startup(SimpleNamespace(bot=bot)))
    assert len(bot.sent) == 1


def test_startup_warns_when_meera_has_not_pressed_start(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import ingest

    async def none_pending(bot) -> int:
        return 0

    monkeypatch.setattr(ingest, "transcribe_pending", none_pending)
    _pending_draft()
    bot = StartupBot(reachable=False)
    asyncio.run(app.on_startup(SimpleNamespace(bot=bot)))
    assert bot.sent == []
    assert "review_chat=unreachable" in caplog.text and "press Start" in caplog.text


def test_asset_check_passes_and_fails_fast_on_missing_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    app.check_assets()
    monkeypatch.setattr(app, "REQUIRED_PROMPTS", ("does_not_exist",))
    with pytest.raises(FileNotFoundError):
        app.check_assets()


# --- app routing ---------------------------------------------------------------------------


def _handlers() -> list:
    return app.build_application().handlers[0]


def _text_update(user_id: int, chat_id: int, text: str = "hello") -> Update:
    chat_type = Chat.PRIVATE if chat_id > 0 else Chat.GROUP
    # Real Telegram commands carry a bot_command entity, which is what filters.COMMAND checks.
    entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text))] if text.startswith("/") else None
    message = Message(message_id=5, date=T0, chat=Chat(id=chat_id, type=chat_type), text=text, entities=entities,
                      from_user=User(id=user_id, first_name="x", is_bot=False))
    return Update(update_id=1, message=message)


def test_review_message_handler_only_accepts_meera_in_review_chat() -> None:
    handler = next(h for h in _handlers() if getattr(h, "callback", None) is review.handle_review_message)
    assert handler.check_update(_text_update(MEERA, REVIEW_CHAT))
    assert not handler.check_update(_text_update(STRANGER, REVIEW_CHAT))
    assert not handler.check_update(_text_update(MEERA, -100777))
    assert not handler.check_update(_text_update(MEERA, REVIEW_CHAT, "/run"))


def test_callback_handler_only_matches_known_actions() -> None:
    handler = next(h for h in _handlers() if getattr(h, "callback", None) is review.handle_callback)
    user = User(id=MEERA, first_name="m", is_bot=False)

    def query(data: str) -> Update:
        return Update(update_id=1, callback_query=CallbackQuery(id="q", from_user=user, chat_instance="c", data=data))

    assert handler.check_update(query("approve:12"))
    assert not handler.check_update(query("publish:12"))


# --- v2: final version storage, posted status, regenerate ------------------------------------


def test_approved_ai_draft_is_stored_as_final() -> None:
    d = _pending_draft()
    query = _press(f"approve:{d.id}")
    final = db.get_final_post(d.id)
    assert (final.final_body, final.ai_draft_body, final.edited_by_meera) == (BODY, BODY, False)
    assert final.publishing_status == "awaiting_manual_post"
    [markup] = query.message.markups
    assert markup.inline_keyboard[0][0].callback_data == f"posted:{d.id}"


def test_meera_edit_is_stored_separately_from_ai_draft() -> None:  # TEST 10
    d = _pending_draft()
    _press(f"edit:{d.id}")
    _say(REWRITE)
    edited = db.get_draft(d.id + 1)
    _press(f"approve:{edited.id}")
    final = db.get_final_post(edited.id)
    assert final.final_body == REWRITE.strip()
    assert final.ai_draft_body == BODY  # the AI version she replaced is kept for feedback
    assert final.edited_by_meera is True
    assert db.get_draft(d.id).body == BODY and db.get_draft(d.id).status == "superseded"


def test_mark_posted_once_and_only_after_approval() -> None:
    d = _pending_draft()
    assert _press(f"posted:{d.id}").answers == [("Already marked, or not an approved draft.", False)]
    _press(f"approve:{d.id}")
    assert _press(f"posted:{d.id}").answers == [("Marked as posted", False)]
    assert db.get_final_post(d.id).publishing_status == "posted"
    assert _press(f"posted:{d.id}").answers == [("Already marked, or not an approved draft.", False)]


def test_stranger_cannot_mark_posted_or_regenerate() -> None:
    d = _pending_draft()
    for action in ("posted", "regen", "reject"):
        assert _press(f"{action}:{d.id}", user_id=STRANGER).answers == [(review.NOT_AUTHORISED, True)]
    assert db.get_draft(d.id).status == "pending_review"


def test_legacy_discard_button_still_rejects() -> None:
    d = _pending_draft()
    _press(f"discard:{d.id}")
    assert db.get_draft(d.id).status == "discarded"


def _regen_result(body: str = "A regenerated body. " * 15) -> draft.DraftResult:
    return draft.DraftResult(body, "https://news.google.com/n", [1], "gemini-3.5-flash",
                             {"headline": "h", "source": "Mint", "date": "22 Sep 2026",
                              "url": "https://news.google.com/n", "relevance": "r"}, {"passed": True})


def test_regenerate_creates_new_checked_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_draft_note(*args):
        calls.append(args)
        return _regen_result()

    monkeypatch.setattr(draft, "draft_note", fake_draft_note)
    d = _pending_draft()
    bot = FakeBot()
    query = FakeQuery(f"regen:{d.id}")
    asyncio.run(review.handle_callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot=bot)))
    new = db.get_draft(d.id + 1)
    assert db.get_draft(d.id).status == "superseded"
    assert (new.revision, new.status, new.source_url) == (2, "pending_review", "https://news.google.com/n")
    assert new.news["source"] == "Mint" and new.qa == {"passed": True}
    assert "NEWS CONTEXT\nHeadline: h\nSource: Mint" in bot.sent[0]["text"]
    assert calls[0][0] == db.get_note(d.note_id).content


def test_regenerate_respects_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    async def fake_draft_note(*args):
        return _regen_result()

    monkeypatch.setattr(draft, "draft_note", fake_draft_note)
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, max_regenerations=1))
    d = _pending_draft()
    _press(f"regen:{d.id}")
    blocked = _press(f"regen:{d.id + 1}")
    assert blocked.answers[0][0].startswith("Regeneration limit reached")
    assert db.get_draft(d.id + 1).status == "pending_review"


@pytest.mark.parametrize(("outcome", "text"), [
    (None, "couldn't produce a new draft"),
    (gemini_client.GeminiError("down"), "couldn't reach the AI service"),
])
def test_failed_regeneration_keeps_current_draft(monkeypatch: pytest.MonkeyPatch, outcome, text: str) -> None:
    async def fake_draft_note(*args):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(draft, "draft_note", fake_draft_note)
    d = _pending_draft()
    query = _press(f"regen:{d.id}")
    assert text in query.message.replies[-1][0]
    assert db.get_draft(d.id).status == "pending_review"


# --- v2: scorecard messages -------------------------------------------------------------------


def _result(decision: str, overall: float, flags=(), model: str = "gemini-3.5-flash-lite"):
    import triage

    def score(p):
        failing = p.key == "postability"
        return triage.ParameterScore(p.key, p.name, p.weight, 9.0, 2.0 if failing else 9.0, "r",
                                     "the finished product pH dropped", True, f"gap {p.name}",
                                     "fail" if failing else "pass", "", ("guardrail fail",) if failing else ())

    params = tuple(score(p) for p in triage.PARAMETERS)
    return triage.TriageResult(1, decision, overall, params, tuple(flags),
                               {"core_idea": "idea", "founder_perspective": "fp", "intended_audience": "aud",
                                "main_insight": "insight", "topic": "t"}, "Industry Transparency", "a", "k", model,
                               ("Postability: gap Postability",))


def test_scorecard_messages_show_all_parameters_and_decision() -> None:
    import triage

    note_id = db.add_note(1, -1001, "", T0, "voice", "f", "pending_transcription")
    db.set_note_transcript(note_id, "batch fourteen came back and the pH drifted", "high", 0.0)
    note = db.get_note(note_id)
    result = _result("rejected", 7.9)
    head = review.format_transcript_message(note, result)
    assert head.startswith(f"Note #{note.id} · voice note · " + review.DECISION_LABELS["rejected"])
    assert "TRANSCRIPT\nbatch fourteen came back" in head and "Core idea: idea" in head
    assert "clarity: high" in head
    card = review.format_scorecard_message(result)
    for p in triage.PARAMETERS:
        assert f"{p.name}: " in card
    assert "Postability: 2/10 · ×1.5 · fail (capped from 9: guardrail fail)" in card
    assert "OVERALL SCORE: 7.9/10" in card and "What would make it stronger" in card
    assert len(card) <= review.TELEGRAM_TEXT_LIMIT and len(head) <= review.TELEGRAM_TEXT_LIMIT


def test_human_review_scorecard_lists_flags_and_no_draft() -> None:
    flag = {"type": "private_customer_information", "detail": "names a customer", "quote": "Priya Sharma"}
    card = review.format_scorecard_message(_result("human_review", 9.2, [flag]))
    assert "DECISION: " + review.DECISION_LABELS["human_review"] in card
    assert "- private customer information: names a customer" in card
    assert "Nothing will be drafted" in card


def test_qualified_scorecard_says_draft_follows() -> None:
    card = review.format_scorecard_message(_result("qualified", 8.6))
    assert "DECISION: QUALIFIED FOR DRAFT" in card and "Drafting now" in card


def test_send_scorecard_records_message_and_survives_telegram_failure() -> None:
    note = db.get_note(db.add_note(1, -1001, "a note long enough to score properly here", T0))
    assessment_id = db.add_assessment(note.id, "m", "v2", "t", {}, [], [], 7.0, "rejected")
    bot = FakeBot()
    assert asyncio.run(review.send_scorecard(bot, note, _result("rejected", 7.0), assessment_id)) is True
    assert len(bot.sent) == 2 and db.get_latest_assessment(note.id).scorecard_message_id == 1002
    failing = FakeBot([BadRequest("x")])
    assert asyncio.run(review.send_scorecard(failing, note, _result("rejected", 7.0), None)) is False
