import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telegram import Chat, Message, PhotoSize, Sticker, Update, Voice

import app
import config
import db
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


def _handle(update: Update) -> None:
    asyncio.run(ingest.handle_channel_post(update, None))


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


def test_voice_post_stored_pending_transcription() -> None:
    _handle(Update(update_id=1, channel_post=_post(voice=_voice())))
    [note] = _all_notes()
    assert (note.content_type, note.status, note.tg_file_id) == ("voice", "pending_transcription", "AwACAgUAAxkBVOICE")
    assert db.get_new_notes() == []


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
    [handler] = app.build_application().handlers[0]
    return handler


def test_app_routes_only_capture_channel_posts() -> None:
    handler = _handler()
    assert handler.check_update(Update(update_id=1, channel_post=_post(text="x")))
    assert not handler.check_update(Update(update_id=2, channel_post=_post(chat_id=OTHER, text="x")))
    assert not handler.check_update(Update(update_id=3, edited_channel_post=_post(text="x")))
    private = Message(message_id=1, date=WHEN, chat=Chat(id=5, type=Chat.PRIVATE), text="x")
    assert not handler.check_update(Update(update_id=4, message=private))


def test_polling_asks_only_for_channel_posts() -> None:
    assert app.ALLOWED_UPDATES == ["channel_post"]


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
