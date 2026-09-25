"""Per-note pipeline: triage -> scorecard -> (qualified only) news + draft + QA -> review.

Runs the moment a note is ready (text posted, or voice transcribed), plus a periodic sweep
that retries anything that failed. It never raises: failures are logged, the note stays
retryable, and Meera gets a short notice with no error details.
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
import review
import triage

log = logging.getLogger(__name__)

NOTICE_AI_DOWN = ("I couldn't process your latest note because the AI service wasn't reachable. "
                  "I'll retry automatically.")
NOTICE_DRAFT_DROPPED = ("Your note qualified, but I couldn't produce a draft I could verify against it, "
                        "so nothing was sent. The note is shelved.")
NOTICE_ERROR = "Something went wrong processing your latest note. I'll retry automatically."

SWEEP_REPLIES = {
    "drafted": "Done. New drafts are above.",
    "no_candidate": "Nothing was waiting to be processed.",
    "processed": "Done. Scorecards are above; nothing qualified for a draft.",
    "error": "Some notes couldn't be processed yet. I'll retry automatically.",
    "skipped": "A sweep for this minute already happened.",
}

# One note at a time: an arrival task and a sweep must never process the same note concurrently.
_note_lock = asyncio.Lock()
_sweep_lock = asyncio.Lock()


def sweep_slot(now: datetime, prefix: str = "sweep") -> str:
    local = now.astimezone(config.settings.timezone)
    return f"{prefix}-{local:%Y-%m-%d-%H%M}"


async def process_note(bot: Bot, note_id: int) -> str:
    """Process one ready note. Returns drafted | rejected | human_review | dropped | error | skipped."""
    async with _note_lock:
        try:
            return await _process(bot, note_id)
        except gemini_client.GeminiError as exc:
            log.warning("pipeline.ai_unavailable note_id=%s error=%s", note_id, exc)
            await review.send_notice(bot, NOTICE_AI_DOWN)
            return "error"
        except Exception:
            log.exception("pipeline.unexpected_error note_id=%s", note_id)
            await review.send_notice(bot, NOTICE_ERROR)
            return "error"


async def _process(bot: Bot, note_id: int) -> str:
    note = await asyncio.to_thread(db.get_note, note_id)
    if note is None or note.status != "new":
        return "skipped"

    result = await triage.assess_note(note)  # reuses a stored assessment; GeminiError propagates
    if result is None:
        return "error"  # unusable model output; left as 'new' for the sweep
    assessment = await asyncio.to_thread(db.get_latest_assessment, note.id)
    if assessment.scorecard_message_id is None:
        if not await review.send_scorecard(bot, note, result, assessment.id):
            return "error"  # keep the note retryable until Meera has actually seen the verdict

    if not result.qualified:
        await asyncio.to_thread(db.set_note_status, note.id, "shelved")
        log.info("pipeline.not_drafted note_id=%s decision=%s overall=%.1f", note.id, result.decision, result.overall)
        return result.decision

    drafted = await draft.draft_note(note.content, result.category, result.angle,
                                     result.summary.get("core_idea", ""), result.news_keywords)
    if drafted is None:
        await asyncio.to_thread(db.set_note_status, note.id, "shelved")  # fail closed
        await review.send_notice(bot, NOTICE_DRAFT_DROPPED)
        return "dropped"

    draft_id = await asyncio.to_thread(db.add_draft, note.id, drafted.body, drafted.model, drafted.exemplar_ids,
                                       drafted.source_url, drafted.news, drafted.qa)
    await asyncio.to_thread(db.set_note_status, note.id, "drafted")
    delivered = await review.send_for_review(bot, draft_id)
    log.info("pipeline.drafted note_id=%s draft_id=%s news=%s delivered=%s",
             note.id, draft_id, "yes" if drafted.news else "no", delivered)
    return "drafted"


async def run_sweep(bot: Bot, slot: str) -> str:
    """Retry everything outstanding: transcriptions, undelivered drafts, unprocessed notes. Never raises."""
    async with _sweep_lock:
        try:
            run_id = await asyncio.to_thread(db.start_run, slot)
        except Exception:
            log.exception("pipeline.sweep_start_failed slot=%s", slot)
            return "error"
        if run_id is None:
            return "skipped"
        outcomes: list[str] = []
        try:
            await ingest.transcribe_pending(bot)
            await review.deliver_waiting(bot)
            for note in await asyncio.to_thread(db.get_new_notes):
                outcomes.append(await process_note(bot, note.id))
        except Exception:
            log.exception("pipeline.sweep_failed slot=%s", slot)
            outcomes.append("error")
        if "error" in outcomes:
            outcome = "error"
        elif "drafted" in outcomes:
            outcome = "drafted"
        else:
            outcome = "no_candidate"
        detail = ", ".join(f"{o}={outcomes.count(o)}" for o in sorted(set(outcomes))) or "nothing pending"
        try:
            await asyncio.to_thread(db.finish_run, run_id, outcome, detail)
        except Exception:
            log.exception("pipeline.sweep_finish_failed slot=%s", slot)
        log.info("pipeline.sweep slot=%s outcome=%s detail=%s", slot, outcome, detail)
        if outcome == "no_candidate" and outcomes:
            return "processed"
        return outcome


async def sweep_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_sweep(context.bot, sweep_slot(datetime.now(UTC)))


async def handle_run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/run: Meera asks for anything pending to be processed now (the handler filter also restricts this)."""
    message = update.message
    if message is None or not review.is_meera(message.from_user):
        return
    await review.reply_safely(message, "Processing anything pending now...")
    outcome = await run_sweep(context.bot, sweep_slot(datetime.now(UTC), prefix="manual"))
    await review.reply_safely(message, SWEEP_REPLIES[outcome])
