import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import db

CHAT = -1001234567890
T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _note(message_id: int = 1, content: str = "batch fourteen came back with a pH drift", **kw) -> int:
    note_id = db.add_note(message_id, CHAT, content, kw.pop("created_at", T0), **kw)
    assert note_id is not None
    return note_id


# --- schema --------------------------------------------------------------------


def test_init_creates_tables_and_is_repeatable(fresh_db: Path) -> None:
    db.init_db(fresh_db)  # second run must not fail or re-apply migrations
    assert db.schema_version() == len(db.MIGRATIONS)
    with sqlite3.connect(fresh_db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"notes", "drafts", "runs"} <= tables


def test_init_creates_missing_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "volume" / "data" / "app.db"
    db.init_db(path)
    assert path.exists()


def test_using_db_before_init_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    with pytest.raises(RuntimeError, match="init_db"):
        db.get_new_notes()


def test_data_survives_reopening(fresh_db: Path) -> None:
    note_id = _note()
    db.init_db(fresh_db)
    assert db.get_note(note_id) is not None


# --- notes ---------------------------------------------------------------------


def test_add_note_stores_fields() -> None:
    note = db.get_note(_note(content="  a note with padding  "))
    assert note is not None
    assert note.content == "a note with padding"
    assert (note.content_type, note.status) == ("text", "new")
    assert note.tg_chat_id == CHAT
    assert note.created_at == T0
    assert note.category is None and note.score is None


def test_duplicate_telegram_message_is_a_no_op() -> None:
    first = _note(message_id=481)
    again = db.add_note(481, CHAT, "redelivered copy", T0)
    assert again is None
    assert [n.id for n in db.get_new_notes()] == [first]
    assert db.get_note(first).content == "batch fourteen came back with a pH drift"


def test_same_message_id_in_a_different_chat_is_a_new_note() -> None:
    _note(message_id=5)
    assert db.add_note(5, -1009999999999, "note from a new capture channel", T0) is not None


@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_empty_note_rejected(content: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        db.add_note(1, CHAT, content, T0)


def test_voice_note_pending_transcription_may_be_empty() -> None:
    note = db.get_note(_note(content="", content_type="voice", tg_file_id="AwACAgUAAx",
                             status="pending_transcription"))
    assert (note.content_type, note.status, note.tg_file_id) == ("voice", "pending_transcription", "AwACAgUAAx")
    assert db.get_new_notes() == []  # not ready for triage until transcribed


def test_unsupported_post_stored_shelved_without_content() -> None:
    note = db.get_note(_note(content="", content_type="unsupported", status="shelved"))
    assert (note.content_type, note.status, note.content) == ("unsupported", "shelved", "")
    assert db.get_new_notes() == []


def test_invalid_enum_values_rejected() -> None:
    with pytest.raises(ValueError, match="content_type"):
        db.add_note(1, CHAT, "x", T0, content_type="sticker")
    with pytest.raises(ValueError, match="status"):
        db.add_note(1, CHAT, "x", T0, status="published")


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        db.add_note(1, CHAT, "x", datetime(2026, 9, 21, 7, 0))


def test_get_new_notes_filters_and_orders_oldest_first() -> None:
    late = _note(message_id=1, created_at=T0 + timedelta(hours=2))
    early = _note(message_id=2, created_at=T0)
    drafted = _note(message_id=3, created_at=T0 - timedelta(hours=1))
    db.set_note_status(drafted, "drafted")
    assert [n.id for n in db.get_new_notes()] == [early, late]


def test_note_status_transitions() -> None:
    note_id = _note()
    assert db.set_note_status(note_id, "drafted") is True
    assert db.get_note(note_id).status == "drafted"
    assert db.set_note_status(note_id, "shelved") is True
    assert db.get_note(note_id).status == "shelved"


def test_note_status_invalid_value_or_unknown_id() -> None:
    note_id = _note()
    with pytest.raises(ValueError):
        db.set_note_status(note_id, "published")
    assert db.set_note_status(9999, "drafted") is False
    assert db.get_note(9999) is None


def test_sql_metacharacters_stored_literally() -> None:
    nasty = "Robert'); DROP TABLE notes; -- and a % and a \" quote"
    note = db.get_note(_note(content=nasty))
    assert note.content == nasty
    assert len(db.get_new_notes()) == 1


# --- drafts --------------------------------------------------------------------


def test_add_draft_and_read_back() -> None:
    note_id = _note()
    draft = db.get_draft(db.add_draft(note_id, "Draft body", "gemini-3.5-flash", [3, 9], "https://example.com/a"))
    assert draft is not None
    assert (draft.note_id, draft.revision, draft.status) == (note_id, 1, "pending_review")
    assert draft.exemplar_ids == [3, 9]
    assert draft.source_url == "https://example.com/a"
    assert draft.model == "gemini-3.5-flash"
    assert draft.reviewed_at is None


def test_revisions_increment_per_note() -> None:
    a, b = _note(message_id=1), _note(message_id=2)
    assert db.get_draft(db.add_draft(a, "a1", "m")).revision == 1
    assert db.get_draft(db.add_draft(a, "a2", "m")).revision == 2
    assert db.get_draft(db.add_draft(b, "b1", "m")).revision == 1


def test_draft_defaults_for_optional_fields() -> None:
    draft = db.get_draft(db.add_draft(_note(), "body", "m"))
    assert draft.exemplar_ids == [] and draft.source_url is None


def test_draft_requires_existing_note() -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.add_draft(9999, "orphan", "m")


def test_empty_draft_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        db.add_draft(_note(), "  ", "m")


@pytest.mark.parametrize("status", ["approved", "discarded", "superseded"])
def test_draft_leaves_pending_review_once(status: str) -> None:
    draft_id = db.add_draft(_note(), "body", "m")
    assert db.set_draft_status(draft_id, status) is True
    draft = db.get_draft(draft_id)
    assert draft.status == status and draft.reviewed_at is not None
    # A double-tapped button must not act twice.
    assert db.set_draft_status(draft_id, "approved") is False
    assert db.get_draft(draft_id).status == status


def test_draft_status_guards() -> None:
    draft_id = db.add_draft(_note(), "body", "m")
    with pytest.raises(ValueError):
        db.set_draft_status(draft_id, "pending_review")
    with pytest.raises(ValueError):
        db.set_draft_status(draft_id, "published")
    assert db.set_draft_status(9999, "approved") is False


# --- runs ----------------------------------------------------------------------


def test_run_slot_is_claimed_once() -> None:
    run_id = db.start_run("2026-09-23-am")
    assert run_id is not None
    assert db.start_run("2026-09-23-am") is None  # scheduler double-fire
    assert db.start_run("2026-09-25-am") is not None


def test_finish_run_records_outcome_once() -> None:
    run_id = db.start_run("2026-09-23-am")
    assert db.finish_run(run_id, "drafted", "note 12") is True
    run = db.get_run("2026-09-23-am")
    assert (run.outcome, run.detail) == ("drafted", "note 12")
    assert run.finished_at is not None and run.finished_at >= run.started_at
    assert db.finish_run(run_id, "error") is False
    assert db.get_run("2026-09-23-am").outcome == "drafted"


def test_run_guards() -> None:
    run_id = db.start_run("slot")
    with pytest.raises(ValueError):
        db.finish_run(run_id, "exploded")
    with pytest.raises(ValueError):
        db.start_run("  ")
    assert db.finish_run(9999, "error") is False
    assert db.get_run("never") is None


def test_unfinished_run_is_visible() -> None:
    db.start_run("slot")
    run = db.get_run("slot")
    assert run.outcome is None and run.finished_at is None
