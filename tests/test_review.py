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
        self.markup_removed = False

    async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
        self.replies.append((text, parse_mode))

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
    buttons = [b.callback_data for b in sent["reply_markup"].inline_keyboard[0]]
    assert buttons == [f"approve:{d.id}", f"edit:{d.id}", f"discard:{d.id}"]
    assert sent["text"].startswith(f"Draft #{d.id} · rev 1 · Industry Transparency · score 8.5/10")
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
    assert bot.sent and bot.sent[0]["text"].startswith(f"Draft #{new.id} · rev 2")


def test_overlong_rewrite_is_refused_without_changes() -> None:
    d = _pending_draft()
    _press(f"edit:{d.id}")
    message = _say("x" * (config.settings.draft_max_chars + 1))
    assert "trim" in message.replies[0][0]
    assert db.get_draft(d.id).status == "pending_review" and db.get_awaiting_edit().id == d.id


def test_short_instruction_triggers_one_validated_redraft(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    async def fake_revise(note_text: str, previous: str, instruction: str) -> str:
        seen.update(note=note_text, previous=previous, instruction=instruction)
        return "Revised body. " * 20

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
    assert [m["text"].split(" · ")[0] for m in bot.sent] == [f"Draft #{d.id}"]
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
