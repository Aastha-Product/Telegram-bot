import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, PhotoSize, Sticker, Update, Voice
from telegram.error import NetworkError

import app
import config
import db
import gemini_client
import ingest

CAPTURE = config.settings.telegram_chat_id
OTHER = -1009999999999
WHEN = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path) -> None:
    db.init_db(tmp_path / "test.db")


def _post(message_id: int = 1, chat_id: int = CAPTURE, **fields) -> Message:
    return Message(message_id=message_id, date=WHEN, chat=Chat(id=chat_id, type=Chat.CHANNEL), **fields)


def _voice() -> Voice:
    return Voice(file_id="AwACAgUAAxkBVOICE", file_unique_id="u1", duration=42)


def _photo() -> list[PhotoSize]:
    return [PhotoSize(file_id="p", file_unique_id="pu", width=10, height=10)]


def _sticker() -> Sticker:
    return Sticker(file_id="s", file_unique_id="su", width=1, height=1,
                   is_animated=False, is_video=False, type=Sticker.REGULAR)


class FakeBot:
    """Serves voice-note downloads; `fail=True` simulates Telegram being unreachable."""

    username = "testbot"

    def __init__(self, audio: bytes = b"OggS-fake-audio", fail: bool = False) -> None:
        self.audio, self.fail, self.requested = audio, fail, []

    async def send_chat_action(self, chat_id: int, action: str) -> bool:
        return True

    async def get_file(self, file_id: str) -> SimpleNamespace:
        self.requested.append(file_id)
        if self.fail:
            raise NetworkError("telegram unreachable")

        async def download_as_bytearray() -> bytearray:
            return bytearray(self.audio)

        return SimpleNamespace(download_as_bytearray=download_as_bytearray)


@pytest.fixture
def transcriber(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace Gemini transcription; append a string (transcript) or exception per call."""
    script: list = []

    async def fake_transcribe(audio: bytes, mime_type: str, model: str) -> gemini_client.Transcript:
        assert (mime_type, model) == ("audio/ogg", config.settings.transcribe_model)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, gemini_client.Transcript):
            return item
        return gemini_client.Transcript(item, "high", ["English"])

    monkeypatch.setattr(gemini_client, "transcribe_audio", fake_transcribe)
    return script


def _handle(update: Update, bot: FakeBot | None = None) -> None:
    asyncio.run(ingest.handle_channel_post(update, SimpleNamespace(bot=bot or FakeBot(fail=True))))


def _all_notes() -> list[db.Note]:
    return [n for n in (db.get_note(i) for i in range(1, 50)) if n]


# --- extract_note ----------------------------------------------------------------


def test_extract_text() -> None:
    assert ingest.extract_note(_post(text="  a thought  ")) == ingest.NoteInput("a thought", "text", "new")


def test_extract_blank_text_is_ignored() -> None:
    assert ingest.extract_note(_post(text="   \n ")) is None


def test_extract_voice_pending_transcription() -> None:
    note = ingest.extract_note(_post(voice=_voice()))
    assert note == ingest.NoteInput("", "voice", "pending_transcription", "AwACAgUAAxkBVOICE")


def test_extract_voice_keeps_caption() -> None:
    assert ingest.extract_note(_post(voice=_voice(), caption=" re: batch 14 ")).content == "re: batch 14"


def test_extract_photo_with_caption_uses_caption() -> None:
    assert ingest.extract_note(_post(photo=_photo(), caption="label shot")) == ingest.NoteInput(
        "label shot", "text", "new")


@pytest.mark.parametrize("fields", [{"sticker": _sticker()}, {"photo": _photo()}])
def test_extract_non_text_is_unsupported(fields: dict) -> None:
    assert ingest.extract_note(_post(**fields)) == ingest.NoteInput("", "unsupported", "shelved")


# --- handler ---------------------------------------------------------------------


def test_text_post_is_stored() -> None:
    _handle(Update(update_id=1, channel_post=_post(message_id=481, text="batch fourteen came back")))
    [note] = db.get_new_notes()
    assert (note.tg_message_id, note.tg_chat_id, note.content) == (481, CAPTURE, "batch fourteen came back")
    assert note.created_at == WHEN


def test_redelivered_post_is_stored_once() -> None:
    update = Update(update_id=1, channel_post=_post(message_id=481, text="note"))
    _handle(update)
    _handle(update)
    assert len(_all_notes()) == 1


def test_post_from_another_channel_is_rejected() -> None:
    _handle(Update(update_id=1, channel_post=_post(chat_id=OTHER, text="not Meera's channel")))
    assert _all_notes() == []


def test_non_channel_updates_are_rejected() -> None:
    private = Message(message_id=1, date=WHEN, chat=Chat(id=CAPTURE, type=Chat.PRIVATE), text="hi")
    _handle(Update(update_id=1, message=private))
    _handle(Update(update_id=2, edited_channel_post=_post(text="edited")))
    assert _all_notes() == []


def test_voice_post_transcribed_and_released_to_triage(transcriber: list) -> None:
    transcriber.append("batch fourteen came back and the pH had drifted")
    bot = FakeBot()
    _handle(Update(update_id=1, channel_post=_post(voice=_voice())), bot)
    assert bot.requested == ["AwACAgUAAxkBVOICE"]
    [note] = db.get_new_notes()
    assert (note.content_type, note.status) == ("voice", "new")
    assert note.content == "batch fourteen came back and the pH had drifted"


def test_voice_caption_kept_with_transcript(transcriber: list) -> None:
    transcriber.append("the transcript")
    _handle(Update(update_id=1, channel_post=_post(voice=_voice(), caption="re: batch 14")), FakeBot())
    assert db.get_new_notes()[0].content == "re: batch 14\n\nthe transcript"


def test_voice_stays_pending_when_gemini_fails(transcriber: list, caplog) -> None:
    transcriber.append(gemini_client.GeminiError("model call failed: ServerError 503 UNAVAILABLE"))
    _handle(Update(update_id=1, channel_post=_post(voice=_voice())), FakeBot())
    [note] = _all_notes()
    assert (note.content_type, note.status, note.tg_file_id) == ("voice", "pending_transcription", "AwACAgUAAxkBVOICE")
    assert db.get_new_notes() == []
    assert "ingest.transcribe_failed" in caplog.text


def test_voice_stays_pending_when_download_fails(transcriber: list) -> None:
    _handle(Update(update_id=1, channel_post=_post(voice=_voice())), FakeBot(fail=True))
    assert _all_notes()[0].status == "pending_transcription"
    assert transcriber == []  # model never called without audio


def test_transcribe_pending_retries_failed_voice_notes(transcriber: list) -> None:
    transcriber.append(gemini_client.GeminiError("down"))
    _handle(Update(update_id=1, channel_post=_post(message_id=1, voice=_voice())), FakeBot())
    _handle(Update(update_id=2, channel_post=_post(message_id=2, voice=_voice())), FakeBot(fail=True))
    assert len(db.get_pending_transcriptions()) == 2

    transcriber.extend(["first transcript", gemini_client.GeminiError("down again")])
    assert asyncio.run(ingest.transcribe_pending(FakeBot())) == 1
    assert [n.content for n in db.get_new_notes()] == ["first transcript"]
    assert len(db.get_pending_transcriptions()) == 1


def test_duplicate_voice_post_is_not_transcribed_twice(transcriber: list) -> None:
    transcriber.append("once")
    update = Update(update_id=1, channel_post=_post(voice=_voice()))
    _handle(update, FakeBot())
    _handle(update, FakeBot())  # would pop from an empty script if it re-transcribed
    assert len(_all_notes()) == 1


def test_sticker_stored_unsupported_and_never_triaged() -> None:
    _handle(Update(update_id=1, channel_post=_post(sticker=_sticker())))
    [note] = _all_notes()
    assert (note.content_type, note.status) == ("unsupported", "shelved")
    assert db.get_new_notes() == []


def test_blank_post_stores_nothing() -> None:
    _handle(Update(update_id=1, channel_post=_post(text="   ")))
    assert _all_notes() == []


def test_db_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add_note", boom)
    with caplog.at_level(logging.ERROR):
        _handle(Update(update_id=1, channel_post=_post(message_id=7, text="note")))
    assert "ingest.failed message_id=7" in caplog.text


def test_logs_do_not_contain_note_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        _handle(Update(update_id=1, channel_post=_post(text="private formulation detail")))
    assert "ingest.stored" in caplog.text
    assert "private formulation detail" not in caplog.text


# --- app wiring ------------------------------------------------------------------


def _handler():
    return next(h for h in app.build_application().handlers[0] if h.callback is ingest.handle_channel_post)


def test_app_retries_pending_transcriptions_on_startup(transcriber: list) -> None:
    db.add_note(1, CAPTURE, "", WHEN, "voice", "file-1", "pending_transcription")
    transcriber.append("recovered after restart")
    asyncio.run(app.on_startup(SimpleNamespace(bot=FakeBot())))
    assert db.get_new_notes()[0].content == "recovered after restart"


def test_app_routes_only_capture_channel_posts() -> None:
    handler = _handler()
    assert handler.check_update(Update(update_id=1, channel_post=_post(text="x")))
    assert not handler.check_update(Update(update_id=2, channel_post=_post(chat_id=OTHER, text="x")))
    assert not handler.check_update(Update(update_id=3, edited_channel_post=_post(text="x")))
    private = Message(message_id=1, date=WHEN, chat=Chat(id=5, type=Chat.PRIVATE), text="x")
    assert not handler.check_update(Update(update_id=4, message=private))


def test_polling_asks_only_for_needed_update_types() -> None:
    assert sorted(app.ALLOWED_UPDATES) == ["callback_query", "channel_post", "message"]


def test_redact_filter_scrubs_message_args_and_traceback() -> None:
    secret = "123456:SUPER_SECRET_TOKEN"
    try:
        raise RuntimeError(f"failed calling https://api.telegram.org/bot{secret}/getMe")
    except RuntimeError as exc:
        record = logging.LogRecord("httpx", logging.ERROR, __file__, 1,
                                   "POST https://api.telegram.org/bot%s/getUpdates", (secret,),
                                   (type(exc), exc, exc.__traceback__))
    assert app.RedactSecrets([secret]).filter(record)
    formatted = logging.Formatter().format(record)
    assert secret not in formatted
    assert "bot***/getUpdates" in formatted and "bot***/getMe" in formatted


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("httpx").level)
    yield
    root.handlers[:], root.level = saved[0], saved[1]
    logging.getLogger("httpx").setLevel(saved[2])


@pytest.mark.usefixtures("restore_logging")
def test_setup_logging_silences_httpx_request_urls() -> None:
    app.setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


class _FailingApp:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def run_polling(self, **kwargs) -> None:
        raise self.exc


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("make_exc", [
    lambda t: app.InvalidToken(f"The token `{t}` was rejected by the server."),
    lambda t: RuntimeError(f"connection to https://api.telegram.org/bot{t}/getMe failed"),
])
def test_startup_failure_exits_cleanly_without_leaking_token(
    make_exc, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    token = config.settings.telegram_bot_token
    monkeypatch.setattr(db, "init_db", lambda path=None: None)
    monkeypatch.setattr(app, "build_application", lambda: _FailingApp(make_exc(token)))
    with pytest.raises(SystemExit) as exit_info:
        app.main()
    assert exit_info.value.code == 1
    output = capsys.readouterr()
    assert token not in output.err + output.out
    assert "app.startup_failed" in output.err or "app.crashed" in output.err


# --- sender, transcript confidence, readiness ---------------------------------------


def test_sender_is_recorded() -> None:
    signed = _post(message_id=1, text="a note with enough words", author_signature="Meera Pillai")
    _handle(Update(update_id=1, channel_post=signed))
    assert db.get_new_notes()[0].sender == "signature:Meera Pillai"
    assert ingest.sender_of(_post(text="x", sender_chat=Chat(id=CAPTURE, type=Chat.CHANNEL))) == f"chat:{CAPTURE}"
    assert ingest.sender_of(_post(text="x")) == "unknown"


def test_handler_returns_id_only_when_ready_for_triage(transcriber: list) -> None:
    ready = asyncio.run(ingest.handle_channel_post(
        Update(update_id=1, channel_post=_post(message_id=1, text="text note")), SimpleNamespace(bot=FakeBot())))
    assert ready == 1
    transcriber.append(gemini_client.GeminiError("down"))
    pending = asyncio.run(ingest.handle_channel_post(
        Update(update_id=2, channel_post=_post(message_id=2, voice=_voice())), SimpleNamespace(bot=FakeBot())))
    assert pending is None
    sticker = asyncio.run(ingest.handle_channel_post(
        Update(update_id=3, channel_post=_post(message_id=3, sticker=_sticker())), SimpleNamespace(bot=FakeBot())))
    assert sticker is None


def test_transcript_clarity_and_unclear_ratio_are_stored(transcriber: list) -> None:
    transcriber.append(gemini_client.Transcript("the batch [unclear] came back [unclear] today", "medium", ["English"]))
    _handle(Update(update_id=1, channel_post=_post(voice=_voice())), FakeBot())
    note = db.get_new_notes()[0]
    assert note.transcript_clarity == "medium"
    assert note.unclear_ratio == round(2 / 7, 3)
    assert ingest.is_low_confidence(note)  # 29% unclear > 20% limit


@pytest.mark.parametrize(("clarity", "text", "low"), [
    ("high", "a perfectly clear voice note about pH drift", False),
    ("low", "a perfectly clear voice note about pH drift", True),
    ("medium", "one [unclear] word in a longer clear voice note", False),
])
def test_low_confidence_rule(transcriber: list, clarity: str, text: str, low: bool) -> None:
    transcriber.append(gemini_client.Transcript(text, clarity, ["English"]))
    _handle(Update(update_id=1, channel_post=_post(voice=_voice())), FakeBot())
    assert ingest.is_low_confidence(db.get_new_notes()[0]) is low


def test_text_notes_are_never_low_confidence() -> None:
    _handle(Update(update_id=1, channel_post=_post(text="[unclear] [unclear] typed by hand")))
    assert not ingest.is_low_confidence(db.get_new_notes()[0])
