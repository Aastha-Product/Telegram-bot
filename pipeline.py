"""One draft run: transcribe -> redeliver -> triage -> news -> draft -> review.

Idempotent per slot (runs.slot is UNIQUE), serialised by a lock, one draft per
slot at most, and it never raises: every failure ends as a logged run outcome
plus, where useful, a quiet notice to Meera with no error details.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from telegram import Bot, Update
from telegram.ext import ContextTypes

import config
import db
import draft
import gemini_client
import ingest
import news
import review
import triage

log = logging.getLogger(__name__)

NOTICE_AI_DOWN = ("I couldn't prepare today's draft because the AI service wasn't reachable. "
                  "I'll try again at the next slot.")
NOTICE_NOTHING_READY = "Nothing is ready to draft yet. Drop a few more notes whenever you like."
NOTICE_DRAFT_DROPPED = ("I skipped one note because I couldn't produce a draft I could verify "
                        "against it. Nothing was sent.")
NOTICE_ERROR = "Something went wrong preparing today's draft. I'll try again at the next slot."

RUN_REPLIES = {
    "drafted": "Done. The draft is above.",
    "no_candidate": "No note is ready to draft right now.",
    "error": "I couldn't finish this run. Details are in the logs; I'll try again at the next slot.",
    "skipped": "A run for this minute already happened.",
}

# A scheduled job and a manual /run must never draft from the same note at once.
_run_lock = asyncio.Lock()


def scheduled_slot(now: datetime) -> str:
    local = now.astimezone(config.settings.timezone)
    return f"{local:%Y-%m-%d-%H%M}"


def manual_slot(now: datetime) -> str:
    return f"manual-{scheduled_slot(now)}"


async def run_pipeline(bot: Bot, slot: str) -> str:
    """Run one slot; returns drafted | no_candidate | error | skipped. Never raises."""
    async with _run_lock:
        try:
            run_id = await asyncio.to_thread(db.start_run, slot)
        except Exception:
            log.exception("pipeline.start_failed slot=%s", slot)
            return "error"
        if run_id is None:
            log.info("pipeline.skipped slot=%s reason=already_ran", slot)
            return "skipped"

        log.info("pipeline.started slot=%s run_id=%s", slot, run_id)
        try:
            outcome, detail, notice = await _run(bot)
        except gemini_client.GeminiError as exc:
            outcome, detail, notice = "error", f"gemini unavailable: {exc}", NOTICE_AI_DOWN
        except Exception:
            log.exception("pipeline.unexpected_error slot=%s", slot)
            outcome, detail, notice = "error", "unexpected error (see logs)", NOTICE_ERROR

        try:
            await asyncio.to_thread(db.finish_run, run_id, outcome, detail)
        except Exception:
            log.exception("pipeline.finish_failed slot=%s", slot)
        log.info("pipeline.finished slot=%s outcome=%s detail=%s", slot, outcome, detail)
        if notice:
            await review.send_notice(bot, notice)
        return outcome


async def _run(bot: Bot) -> tuple[str, str, str | None]:
    await ingest.transcribe_pending(bot)

    # A draft that never reached Meera (Telegram was down) takes this slot before any new one.
    undelivered = await asyncio.to_thread(db.get_undelivered_drafts)
    if undelivered:
        pending = undelivered[0]
        if await review.send_for_review(bot, pending.id):
            return "drafted", f"redelivered draft {pending.id}", None
        return "error", f"delivery still failing for draft {pending.id}", None

    notes = await asyncio.to_thread(db.get_new_notes)
    results = await triage.score_notes(notes)
    best = triage.pick_best(results, config.settings.triage_threshold)
    if best is None:
        # Say "nothing ready" once per dry spell, not every slot.
        previous = await asyncio.to_thread(db.get_last_outcome)
        notice = NOTICE_NOTHING_READY if previous != "no_candidate" else None
        return "no_candidate", f"{len(notes)} new notes, none eligible", notice

    note = await asyncio.to_thread(db.get_note, best.note_id)
    items = await news.fetch_news(best.news_keywords)
    result = await draft.make_draft(note.content, best.category, best.angle,
                                    draft.pick_exemplars(best.category), items)
    if result is None:
        # Fail closed, and shelve the note so the next slot moves on to another one.
        await asyncio.to_thread(db.set_note_status, note.id, "shelved")
        return "error", f"draft for note {note.id} failed validation", NOTICE_DRAFT_DROPPED

    draft_id = await asyncio.to_thread(db.add_draft, note.id, result.body, result.model,
                                       result.exemplar_ids, result.source_url)
    await asyncio.to_thread(db.set_note_status, note.id, "drafted")
    delivered = await review.send_for_review(bot, draft_id)
    detail = f"note {note.id} draft {draft_id} news={'yes' if result.source_url else 'no'}"
    return "drafted", detail if delivered else f"{detail} (delivery pending)", None


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_pipeline(context.bot, scheduled_slot(datetime.now(UTC)))


async def handle_run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/run: Meera triggers a slot on demand (the handler filter also restricts this)."""
    message = update.message
    if message is None or not review.is_meera(message.from_user):
        return
    await review.reply_safely(message, "Running a draft now...")
    outcome = await run_pipeline(context.bot, manual_slot(datetime.now(UTC)))
    await review.reply_safely(message, RUN_REPLIES[outcome])
