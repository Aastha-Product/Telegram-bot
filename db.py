"""SQLite access for notes, drafts and runs. All DB access goes through here.

Every query is parameterised. Timestamps are stored as ISO-8601 UTC text so the
schema ports to Postgres mechanically.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import config

NOTE_CONTENT_TYPES: tuple[str, ...] = ("text", "voice", "unsupported")
NOTE_STATUSES: tuple[str, ...] = ("pending_transcription", "new", "drafted", "shelved")
DRAFT_STATUSES: tuple[str, ...] = ("pending_review", "approved", "discarded", "superseded")
RUN_OUTCOMES: tuple[str, ...] = ("drafted", "no_candidate", "error")

# Append-only. Entry N must be one complete transaction ending with `PRAGMA user_version = N`.
MIGRATIONS: tuple[str, ...] = (
    """
    BEGIN;
    CREATE TABLE notes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_message_id INTEGER NOT NULL,
        tg_chat_id    INTEGER NOT NULL,
        content       TEXT    NOT NULL,
        content_type  TEXT    NOT NULL DEFAULT 'text'
                      CHECK (content_type IN ('text', 'voice', 'unsupported')),
        tg_file_id    TEXT,
        created_at    TIMESTAMP NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'new'
                      CHECK (status IN ('pending_transcription', 'new', 'drafted', 'shelved')),
        category      TEXT,
        score         REAL,
        UNIQUE (tg_chat_id, tg_message_id)
    );
    CREATE INDEX idx_notes_status ON notes (status);

    CREATE TABLE drafts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id      INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
        revision     INTEGER NOT NULL,
        body         TEXT    NOT NULL,
        model        TEXT    NOT NULL,
        exemplar_ids TEXT,
        source_url   TEXT,
        status       TEXT    NOT NULL DEFAULT 'pending_review'
                     CHECK (status IN ('pending_review', 'approved', 'discarded', 'superseded')),
        created_at   TIMESTAMP NOT NULL,
        reviewed_at  TIMESTAMP,
        UNIQUE (note_id, revision)
    );

    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slot        TEXT NOT NULL UNIQUE,
        started_at  TIMESTAMP NOT NULL,
        finished_at TIMESTAMP,
        outcome     TEXT CHECK (outcome IN ('drafted', 'no_candidate', 'error')),
        detail      TEXT
    );
    PRAGMA user_version = 1;
    COMMIT;
    """,
    """
    BEGIN;
    ALTER TABLE notes ADD COLUMN worth_developing INTEGER;
    ALTER TABLE notes ADD COLUMN triage_reason TEXT;
    ALTER TABLE notes ADD COLUMN angle TEXT;
    ALTER TABLE notes ADD COLUMN news_keywords TEXT;
    PRAGMA user_version = 2;
    COMMIT;
    """,
    """
    BEGIN;
    ALTER TABLE drafts ADD COLUMN review_message_id INTEGER;
    ALTER TABLE drafts ADD COLUMN awaiting_edit_at TIMESTAMP;
    PRAGMA user_version = 3;
    COMMIT;
    """,
    """
    BEGIN;
    ALTER TABLE notes ADD COLUMN sender TEXT;
    ALTER TABLE notes ADD COLUMN transcript_clarity TEXT;
    ALTER TABLE notes ADD COLUMN unclear_ratio REAL;

    CREATE TABLE assessments (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id              INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
        created_at           TIMESTAMP NOT NULL,
        model                TEXT NOT NULL,
        rubric_version       TEXT NOT NULL,
        topic                TEXT,
        summary_json         TEXT NOT NULL,
        parameters_json      TEXT NOT NULL,
        hard_flags_json      TEXT NOT NULL,
        overall              REAL NOT NULL CHECK (overall BETWEEN 0 AND 10),
        decision             TEXT NOT NULL CHECK (decision IN ('qualified', 'rejected', 'human_review')),
        scorecard_message_id INTEGER
    );
    CREATE INDEX idx_assessments_note ON assessments (note_id);

    ALTER TABLE drafts ADD COLUMN news_json TEXT;
    ALTER TABLE drafts ADD COLUMN qa_json TEXT;

    CREATE TABLE final_posts (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        note_id           INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
        draft_id          INTEGER NOT NULL UNIQUE REFERENCES drafts (id) ON DELETE CASCADE,
        ai_draft_body     TEXT NOT NULL,
        final_body        TEXT NOT NULL,
        edited_by_meera   INTEGER NOT NULL,
        approved_at       TIMESTAMP NOT NULL,
        publishing_status TEXT NOT NULL DEFAULT 'awaiting_manual_post'
                          CHECK (publishing_status IN ('awaiting_manual_post', 'posted')),
        posted_at         TIMESTAMP
    );
    PRAGMA user_version = 4;
    COMMIT;
    """,
)

DECISIONS: tuple[str, ...] = ("qualified", "rejected", "human_review")
MEERA_EDIT_MODEL = "meera-edit"

_db_path: Path | None = None


@dataclass(frozen=True)
class Note:
    id: int
    tg_message_id: int
    tg_chat_id: int
    content: str
    content_type: str
    tg_file_id: str | None
    created_at: datetime
    status: str
    category: str | None
    score: float | None
    worth_developing: bool | None = None
    triage_reason: str | None = None
    angle: str | None = None
    news_keywords: str | None = None
    sender: str | None = None
    transcript_clarity: str | None = None
    unclear_ratio: float | None = None


@dataclass(frozen=True)
class Draft:
    id: int
    note_id: int
    revision: int
    body: str
    model: str
    exemplar_ids: list[int]
    source_url: str | None
    status: str
    created_at: datetime
    reviewed_at: datetime | None
    review_message_id: int | None = None
    awaiting_edit: bool = False
    news: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None


@dataclass(frozen=True)
class Assessment:
    id: int
    note_id: int
    created_at: datetime
    model: str
    rubric_version: str
    topic: str | None
    summary: dict[str, Any]
    parameters: list[dict[str, Any]]
    hard_flags: list[dict[str, Any]]
    overall: float
    decision: str
    scorecard_message_id: int | None


@dataclass(frozen=True)
class FinalPost:
    id: int
    note_id: int
    draft_id: int
    ai_draft_body: str
    final_body: str
    edited_by_meera: bool
    approved_at: datetime
    publishing_status: str
    posted_at: datetime | None


@dataclass(frozen=True)
class Run:
    id: int
    slot: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    detail: str | None


# --- connection & schema -------------------------------------------------------


def init_db(path: Path | None = None) -> None:
    """Point the module at a DB file (default: config) and apply pending migrations."""
    global _db_path
    _db_path = path if path is not None else config.settings.db_path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for expected, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(script)
            applied = conn.execute("PRAGMA user_version").fetchone()[0]
            if applied != expected:
                raise RuntimeError(f"migration {expected} left schema at version {applied}")


def schema_version() -> int:
    with _connect() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    if _db_path is None:
        raise RuntimeError("db.init_db() must be called before using the database")
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:  # commits on success, rolls back on exception
            yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _check(value: str, allowed: tuple[str, ...], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {allowed}, got {value!r}")


# --- notes ---------------------------------------------------------------------


def add_note(
    tg_message_id: int,
    tg_chat_id: int,
    content: str,
    created_at: datetime,
    content_type: str = "text",
    tg_file_id: str | None = None,
    status: str = "new",
    sender: str | None = None,
) -> int | None:
    """Insert a note; return its id, or None if this Telegram message was already stored.

    Telegram redelivers updates, so a duplicate insert must be a silent no-op.
    """
    _check(content_type, NOTE_CONTENT_TYPES, "content_type")
    _check(status, NOTE_STATUSES, "status")
    content = content.strip()
    # Only triage-ready notes need text; pending voice notes and shelved unsupported posts may be empty.
    if not content and status == "new":
        raise ValueError("note content must not be empty")
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO notes (tg_message_id, tg_chat_id, content, content_type,
                               tg_file_id, created_at, status, sender)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tg_chat_id, tg_message_id) DO NOTHING
            RETURNING id
            """,
            (tg_message_id, tg_chat_id, content, content_type, tg_file_id,
             _to_iso(created_at), status, sender),
        ).fetchone()
    return row["id"] if row else None


def get_note(note_id: int) -> Note | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _row_to_note(row) if row else None


def get_new_notes() -> list[Note]:
    """Notes ready for triage, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE status = 'new' ORDER BY created_at, id"
        ).fetchall()
    return [_row_to_note(r) for r in rows]


def get_pending_transcriptions() -> list[Note]:
    """Voice notes still waiting for a transcript, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE status = 'pending_transcription' ORDER BY created_at, id"
        ).fetchall()
    return [_row_to_note(r) for r in rows]


def set_note_transcript(note_id: int, content: str, clarity: str | None = None,
                        unclear_ratio: float | None = None) -> bool:
    """Store a voice note's text and release it to triage; False if it wasn't pending."""
    content = content.strip()
    if not content:
        raise ValueError("transcript must not be empty")
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE notes SET content = ?, status = 'new', transcript_clarity = ?, unclear_ratio = ?
            WHERE id = ? AND status = 'pending_transcription'
            """,
            (content, clarity, unclear_ratio, note_id),
        )
    return cur.rowcount == 1


def set_note_triage(
    note_id: int,
    score: float,
    worth_developing: bool,
    category: str | None,
    reason: str,
    angle: str,
    news_keywords: str,
) -> bool:
    """Persist a triage verdict so a note is never scored twice."""
    if not 0.0 <= score <= 10.0:
        raise ValueError("score must be between 0 and 10")
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE notes SET score = ?, worth_developing = ?, category = ?,
                             triage_reason = ?, angle = ?, news_keywords = ?
            WHERE id = ?
            """,
            (score, int(worth_developing), category, reason, angle, news_keywords, note_id),
        )
    return cur.rowcount == 1


def set_note_status(note_id: int, status: str) -> bool:
    """Return True if the note existed and was updated."""
    _check(status, NOTE_STATUSES, "status")
    with _connect() as conn:
        cur = conn.execute("UPDATE notes SET status = ? WHERE id = ?", (status, note_id))
    return cur.rowcount == 1


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        tg_message_id=row["tg_message_id"],
        tg_chat_id=row["tg_chat_id"],
        content=row["content"],
        content_type=row["content_type"],
        tg_file_id=row["tg_file_id"],
        created_at=_from_iso(row["created_at"]),
        status=row["status"],
        category=row["category"],
        score=row["score"],
        worth_developing=None if row["worth_developing"] is None else bool(row["worth_developing"]),
        triage_reason=row["triage_reason"],
        angle=row["angle"],
        news_keywords=row["news_keywords"],
        sender=row["sender"],
        transcript_clarity=row["transcript_clarity"],
        unclear_ratio=row["unclear_ratio"],
    )


# --- assessments -----------------------------------------------------------------


def add_assessment(
    note_id: int,
    model: str,
    rubric_version: str,
    topic: str | None,
    summary: dict[str, Any],
    parameters: list[dict[str, Any]],
    hard_flags: list[dict[str, Any]],
    overall: float,
    decision: str,
) -> int:
    """Store one complete, auditable triage verdict."""
    _check(decision, DECISIONS, "decision")
    if not 0.0 <= overall <= 10.0:
        raise ValueError("overall must be between 0 and 10")
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO assessments (note_id, created_at, model, rubric_version, topic, summary_json,
                                     parameters_json, hard_flags_json, overall, decision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (note_id, _now(), model, rubric_version, topic, json.dumps(summary),
             json.dumps(parameters), json.dumps(hard_flags), overall, decision),
        ).fetchone()
    return row["id"]


def get_latest_assessment(note_id: int) -> Assessment | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM assessments WHERE note_id = ? ORDER BY id DESC LIMIT 1", (note_id,)
        ).fetchone()
    if row is None:
        return None
    return Assessment(
        id=row["id"],
        note_id=row["note_id"],
        created_at=_from_iso(row["created_at"]),
        model=row["model"],
        rubric_version=row["rubric_version"],
        topic=row["topic"],
        summary=json.loads(row["summary_json"]),
        parameters=json.loads(row["parameters_json"]),
        hard_flags=json.loads(row["hard_flags_json"]),
        overall=row["overall"],
        decision=row["decision"],
        scorecard_message_id=row["scorecard_message_id"],
    )


def set_scorecard_message(assessment_id: int, message_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("UPDATE assessments SET scorecard_message_id = ? WHERE id = ?",
                           (message_id, assessment_id))
    return cur.rowcount == 1


# --- drafts --------------------------------------------------------------------


def add_draft(
    note_id: int,
    body: str,
    model: str,
    exemplar_ids: list[int] | None = None,
    source_url: str | None = None,
    news: dict[str, Any] | None = None,
    qa: dict[str, Any] | None = None,
) -> int:
    """Insert the next revision of a draft for a note; return the new draft id."""
    if not body.strip():
        raise ValueError("draft body must not be empty")
    with _connect() as conn:
        # Revision is computed inside the INSERT so two writers can't pick the same number.
        row = conn.execute(
            """
            INSERT INTO drafts (note_id, revision, body, model, exemplar_ids,
                                source_url, created_at, news_json, qa_json)
            VALUES (?, (SELECT COALESCE(MAX(revision), 0) + 1 FROM drafts WHERE note_id = ?),
                    ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (note_id, note_id, body, model, json.dumps(exemplar_ids or []),
             source_url, _now(), _json_or_none(news), _json_or_none(qa)),
        ).fetchone()
    return row["id"]


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value)


def _loads_or_none(value: str | None) -> dict[str, Any] | None:
    return None if value is None else json.loads(value)


def get_draft(draft_id: int) -> Draft | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return _row_to_draft(row) if row else None


def set_draft_status(draft_id: int, status: str) -> bool:
    """Move a draft out of pending_review; return False if it wasn't pending.

    Only pending drafts can change, so a double-tapped button can't act twice.
    """
    _check(status, DRAFT_STATUSES, "status")
    if status == "pending_review":
        raise ValueError("a draft cannot be moved back to pending_review")
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE drafts SET status = ?, reviewed_at = ?, awaiting_edit_at = NULL
            WHERE id = ? AND status = 'pending_review'
            """,
            (status, _now(), draft_id),
        )
    return cur.rowcount == 1


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        note_id=row["note_id"],
        revision=row["revision"],
        body=row["body"],
        model=row["model"],
        exemplar_ids=json.loads(row["exemplar_ids"] or "[]"),
        source_url=row["source_url"],
        status=row["status"],
        created_at=_from_iso(row["created_at"]),
        reviewed_at=_from_iso(row["reviewed_at"]),
        review_message_id=row["review_message_id"],
        awaiting_edit=row["awaiting_edit_at"] is not None,
        news=_loads_or_none(row["news_json"]),
        qa=_loads_or_none(row["qa_json"]),
    )


def set_review_message(draft_id: int, message_id: int) -> bool:
    """Record that a draft reached the review chat (so it isn't delivered twice)."""
    with _connect() as conn:
        cur = conn.execute("UPDATE drafts SET review_message_id = ? WHERE id = ?", (message_id, draft_id))
    return cur.rowcount == 1


def get_undelivered_drafts() -> list[Draft]:
    """Pending drafts whose review message never got through (Telegram was down), oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM drafts WHERE status = 'pending_review' AND review_message_id IS NULL
            ORDER BY created_at, id
            """
        ).fetchall()
    return [_row_to_draft(r) for r in rows]


def start_edit(draft_id: int) -> bool:
    """Mark one pending draft as waiting for Meera's edit; any other waiting draft is released."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE drafts SET awaiting_edit_at = ? WHERE id = ? AND status = 'pending_review'",
            (_now(), draft_id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute("UPDATE drafts SET awaiting_edit_at = NULL WHERE id != ?", (draft_id,))
    return True


def get_awaiting_edit() -> Draft | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM drafts WHERE status = 'pending_review' AND awaiting_edit_at IS NOT NULL
            ORDER BY awaiting_edit_at DESC LIMIT 1
            """
        ).fetchone()
    return _row_to_draft(row) if row else None


def clear_edit(draft_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE drafts SET awaiting_edit_at = NULL WHERE id = ?", (draft_id,))


def add_revision(previous_id: int, body: str, model: str, qa: dict[str, Any] | None = None,
                 news: dict[str, Any] | None = None, replace_news: bool = False) -> int | None:
    """Supersede a pending draft with a new revision in one transaction; None if it wasn't pending.

    Doing both in one transaction guarantees a note never has two drafts pending review.
    News context carries over unless replace_news is set (a regenerated draft may cite other news).
    """
    if not body.strip():
        raise ValueError("draft body must not be empty")
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE drafts SET status = 'superseded', reviewed_at = ?, awaiting_edit_at = NULL
            WHERE id = ? AND status = 'pending_review'
            """,
            (_now(), previous_id),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            """
            INSERT INTO drafts (note_id, revision, body, model, exemplar_ids, source_url, created_at,
                                news_json, qa_json)
            SELECT note_id, (SELECT MAX(revision) + 1 FROM drafts d2 WHERE d2.note_id = d.note_id),
                   ?, ?, exemplar_ids,
                   CASE WHEN ? THEN ? ELSE source_url END, ?,
                   CASE WHEN ? THEN ? ELSE news_json END, ?
            FROM drafts d WHERE id = ?
            RETURNING id
            """,
            (body, model, replace_news, (news or {}).get("url"), _now(),
             replace_news, _json_or_none(news), _json_or_none(qa), previous_id),
        ).fetchone()
    return row["id"]


def count_revisions(note_id: int) -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM drafts WHERE note_id = ?", (note_id,)).fetchone()[0]


# --- approval & final posts --------------------------------------------------------


def approve_draft(draft_id: int) -> bool:
    """Approve a pending draft and store the final text separately from the AI draft, atomically.

    The AI draft is the latest model-written revision at or before the approved one, so an
    approved Meera rewrite keeps the AI version it replaced (useful later as voice feedback).
    """
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE drafts SET status = 'approved', reviewed_at = ?, awaiting_edit_at = NULL
            WHERE id = ? AND status = 'pending_review'
            """,
            (now, draft_id),
        )
        if cur.rowcount != 1:
            return False
        approved = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        ai = conn.execute(
            """
            SELECT body FROM drafts WHERE note_id = ? AND revision <= ? AND model != ?
            ORDER BY revision DESC LIMIT 1
            """,
            (approved["note_id"], approved["revision"], MEERA_EDIT_MODEL),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO final_posts (note_id, draft_id, ai_draft_body, final_body, edited_by_meera, approved_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (approved["note_id"], draft_id, ai["body"] if ai else approved["body"], approved["body"],
             int(approved["model"] == MEERA_EDIT_MODEL), now),
        )
    return True


def get_final_post(draft_id: int) -> FinalPost | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM final_posts WHERE draft_id = ?", (draft_id,)).fetchone()
    if row is None:
        return None
    return FinalPost(
        id=row["id"], note_id=row["note_id"], draft_id=row["draft_id"], ai_draft_body=row["ai_draft_body"],
        final_body=row["final_body"], edited_by_meera=bool(row["edited_by_meera"]),
        approved_at=_from_iso(row["approved_at"]), publishing_status=row["publishing_status"],
        posted_at=_from_iso(row["posted_at"]),
    )


def mark_posted(draft_id: int) -> bool:
    """Meera confirms she posted it herself; False if unknown or already marked."""
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE final_posts SET publishing_status = 'posted', posted_at = ?
            WHERE draft_id = ? AND publishing_status = 'awaiting_manual_post'
            """,
            (_now(), draft_id),
        )
    return cur.rowcount == 1


# --- runs ----------------------------------------------------------------------


def start_run(slot: str) -> int | None:
    """Claim a schedule slot; return the run id, or None if the slot already ran.

    A redeploy can fire the scheduler twice for one slot; UNIQUE(slot) makes that a no-op.
    """
    if not slot.strip():
        raise ValueError("slot must not be empty")
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO runs (slot, started_at) VALUES (?, ?)
            ON CONFLICT (slot) DO NOTHING
            RETURNING id
            """,
            (slot, _now()),
        ).fetchone()
    return row["id"] if row else None


def finish_run(run_id: int, outcome: str, detail: str | None = None) -> bool:
    """Record a run's outcome; return False if the run doesn't exist or already finished."""
    _check(outcome, RUN_OUTCOMES, "outcome")
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runs SET outcome = ?, detail = ?, finished_at = ?
            WHERE id = ? AND finished_at IS NULL
            """,
            (outcome, detail, _now(), run_id),
        )
    return cur.rowcount == 1


def get_last_outcome() -> str | None:
    """Outcome of the most recent finished run (used to avoid repeating 'nothing ready' notices)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT outcome FROM runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return row["outcome"] if row else None


def get_run(slot: str) -> Run | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE slot = ?", (slot,)).fetchone()
    if not row:
        return None
    return Run(
        id=row["id"],
        slot=row["slot"],
        started_at=_from_iso(row["started_at"]),
        finished_at=_from_iso(row["finished_at"]),
        outcome=row["outcome"],
        detail=row["detail"],
    )
