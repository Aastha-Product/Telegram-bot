"""Review gate: scorecards and drafts to Meera, and her Approve / Edit / Reject / Regenerate presses.

This is the terminal step. Nothing here, or anywhere else, publishes: Approve only hands
Meera clean text to post on LinkedIn herself, and she can mark it posted afterwards.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeVar

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

import config
import db
import draft
import gemini_client
import triage

log = logging.getLogger(__name__)

T = TypeVar("T")

# "discard" is kept so buttons on drafts sent before the rename still work.
CALLBACK_RE = re.compile(r"^(approve|edit|reject|discard|regen|posted):(\d{1,12})$")
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
TELEGRAM_TEXT_LIMIT = 4096
MEERA_EDIT_MODEL = db.MEERA_EDIT_MODEL
TRANSCRIPT_PREVIEW_CHARS = 1500
EVIDENCE_PREVIEW_CHARS = 90
GAP_PREVIEW_CHARS = 110

NOT_AUTHORISED = "Only Meera can review drafts."
ALREADY_HANDLED = "This draft was already handled."

DECISION_LABELS = {
    triage.QUALIFIED: "QUALIFIED FOR DRAFT",
    triage.REJECTED: "REJECTED — SCORE TOO LOW",
    triage.HUMAN_REVIEW: "HUMAN REVIEW REQUIRED — GUARDRAIL",
}


def is_meera(user: User | None) -> bool:
    """Authorisation is always checked server-side against the configured id."""
    return user is not None and user.id == config.settings.meera_user_id


def review_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Approve", callback_data=f"approve:{draft_id}"),
         InlineKeyboardButton("Edit", callback_data=f"edit:{draft_id}")],
        [InlineKeyboardButton("Reject", callback_data=f"reject:{draft_id}"),
         InlineKeyboardButton("Regenerate", callback_data=f"regen:{draft_id}")],
    ])


def posted_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("I've posted it", callback_data=f"posted:{draft_id}")]])


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fit(text: str) -> str:
    return text if len(text) <= TELEGRAM_TEXT_LIMIT else text[: TELEGRAM_TEXT_LIMIT - 1] + "…"


# --- scorecard ---------------------------------------------------------------------------


def format_transcript_message(note: db.Note, result: triage.TriageResult) -> str:
    kind = "voice note" if note.content_type == "voice" else "text note"
    s = result.summary
    lines = [f"Note #{note.id} · {kind} · {DECISION_LABELS[result.decision]}",
             f"Overall {result.overall:.1f}/10 (a draft needs more than {config.settings.triage_threshold:g})",
             "", "TRANSCRIPT", _clip(note.content, TRANSCRIPT_PREVIEW_CHARS)]
    if note.content_type == "voice" and note.transcript_clarity:
        lines.append(f"(transcription clarity: {note.transcript_clarity}, model estimate)")
    if result.model != "code":
        lines += ["", "CONTENT SUMMARY",
                  f"Core idea: {s.get('core_idea', '')}",
                  f"Founder perspective: {s.get('founder_perspective', '')}",
                  f"Intended audience: {s.get('intended_audience', '')}",
                  f"Main insight: {s.get('main_insight', '')}"]
    return _fit("\n".join(lines))


def format_scorecard_message(result: triage.TriageResult) -> str:
    lines = ["PUBLISHABILITY SCORECARD (score · weight · guardrail)"]
    for p in result.parameters:
        capped = f" (capped from {p.raw_score:g}: {', '.join(p.caps)})" if p.caps else ""
        lines.append(f"{p.name}: {p.score:g}/10 · ×{p.weight:g} · {p.guardrail}{capped}")
        if p.evidence:
            lines.append(f'   evidence: "{_clip(p.evidence, EVIDENCE_PREVIEW_CHARS)}"')
        if p.gap:
            lines.append(f"   gap: {_clip(p.gap, GAP_PREVIEW_CHARS)}")
    lines += ["", f"OVERALL SCORE: {result.overall:.1f}/10", f"DECISION: {DECISION_LABELS[result.decision]}"]
    if result.hard_flags:
        lines += ["", "GUARDRAIL FLAGS"]
        for f in result.hard_flags:
            quote = f' ("{_clip(f["quote"], 60)}")' if f.get("quote") else ""
            lines.append(f"- {f['type'].replace('_', ' ')}: {_clip(f['detail'], 120)}{quote}")
        lines += ["", "Nothing will be drafted from this note. If you want to go ahead, send a revised "
                      "note without the flagged details."]
    elif result.decision == triage.REJECTED:
        lines += ["", "Not drafted. What would make it stronger:"]
        lines += [f"- {_clip(tip, 160)}" for tip in result.improvements]
    else:
        lines += ["", "Drafting now. The draft will follow for your review."]
    return _fit("\n".join(lines))


def format_suggestions_message(suggestions: list[dict[str, str]]) -> str:
    lines = ["This note isn't strong enough to post yet. Topics that would suit you better:"]
    for n, s in enumerate(suggestions, start=1):
        lines += ["", f"{n}. {s['topic']}" + (f" ({s['category']})" if s.get("category") else "")]
        if s.get("why_it_fits"):
            lines.append(f"   Why it fits you: {s['why_it_fits']}")
        lines.append(f"   Ask yourself: {s['question']}")
        if s.get("news_url"):
            lines.append(f"   In the news: {s['news_headline']} ({s['news_source']}) {s['news_url']}")
    lines += ["", "Send a voice note answering any of these and I'll score it again."]
    return _fit("\n".join(lines))


async def send_scorecard(bot: Bot, note: db.Note, result: triage.TriageResult, assessment_id: int | None,
                         suggestions: list[dict[str, str]] | None = None) -> bool:
    """Transcript + summary, then the scorecard + decision, then topic suggestions if any. Never raises."""
    chat_id = config.settings.telegram_review_chat_id
    try:
        await with_retry(lambda: bot.send_message(chat_id=chat_id, text=format_transcript_message(note, result)))
        card: Message = await with_retry(lambda: bot.send_message(chat_id=chat_id,
                                                                  text=format_scorecard_message(result)))
        if suggestions:
            await with_retry(lambda: bot.send_message(chat_id=chat_id,
                                                      text=format_suggestions_message(suggestions)))
    except TelegramError as exc:
        log.error("review.scorecard_failed note_id=%s error=%s", note.id, type(exc).__name__)
        return False
    if assessment_id is not None:
        await asyncio.to_thread(db.set_scorecard_message, assessment_id, card.message_id)
    log.info("review.scorecard_sent note_id=%s decision=%s", note.id, result.decision)
    return True


# --- drafts ------------------------------------------------------------------------------


def format_for_review(d: db.Draft, note: db.Note | None) -> str:
    meta = [f"Draft #{d.id}", f"rev {d.revision}"]
    if note is not None and note.category:
        meta.append(note.category)
    if note is not None and note.score is not None:
        meta.append(f"score {note.score:g}/10")
    if d.model == MEERA_EDIT_MODEL:
        meta.append("your edit")
    parts = ["LINKEDIN DRAFT · " + " · ".join(meta), d.body]
    if d.news:
        n = d.news
        parts.append("NEWS CONTEXT\n"
                     f"Headline: {n.get('headline', '')}\nSource: {n.get('source', '')} · {n.get('date', '')}\n"
                     f"URL: {n.get('url', '')}\nWhy it's relevant: {n.get('relevance', '')}")
    elif d.source_url:
        parts.append(f"News source used: {d.source_url}")
    else:
        parts.append("NEWS CONTEXT: none used (nothing recent, credible and relevant enough).")
    parts.append("Nothing is posted anywhere unless you post it yourself.")
    return _fit("\n\n".join(parts))


def format_approved(body: str) -> tuple[str, str | None]:
    """Copy-friendly text: a monospace block when it fits Telegram's limit, plain text otherwise."""
    header = "Approved. Copy the text below and post it on LinkedIn yourself."
    block = f"{header}\n\n<pre>{html.escape(body)}</pre>"
    if len(block) <= TELEGRAM_TEXT_LIMIT:
        return block, ParseMode.HTML
    return f"{header}\n\n{body}", None


def _retry_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None if this error must not be retried."""
    if isinstance(exc, RetryAfter):
        wait = exc.retry_after
        return wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
    # BadRequest subclasses NetworkError in PTB, but retrying a malformed request never helps.
    if isinstance(exc, NetworkError) and not isinstance(exc, BadRequest):
        return BACKOFF_BASE_SECONDS * 2**attempt
    return None


async def with_retry(action: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await action()
        except TelegramError as exc:
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt == MAX_RETRIES:
                raise
            log.warning("review.telegram_retry attempt=%d error=%s wait_s=%.1f",
                        attempt + 1, type(exc).__name__, delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def send_for_review(bot: Bot, draft_id: int) -> bool:
    """Deliver a draft with buttons; False leaves it undelivered so the next sweep retries."""
    d = await asyncio.to_thread(db.get_draft, draft_id)
    if d is None or d.status != "pending_review":
        return False
    note = await asyncio.to_thread(db.get_note, d.note_id)
    try:
        message: Message = await with_retry(lambda: bot.send_message(
            chat_id=config.settings.telegram_review_chat_id,
            text=format_for_review(d, note),
            reply_markup=review_keyboard(d.id),
        ))
    except TelegramError as exc:
        log.error("review.delivery_failed draft_id=%s error=%s", draft_id, type(exc).__name__)
        return False
    await asyncio.to_thread(db.set_review_message, d.id, message.message_id)
    log.info("review.sent draft_id=%s note_id=%s revision=%s", d.id, d.note_id, d.revision)
    return True


async def send_notice(bot: Bot, text: str) -> None:
    """A short, quiet status line to Meera. Never raises and never includes error details."""
    try:
        await with_retry(lambda: bot.send_message(chat_id=config.settings.telegram_review_chat_id, text=text))
    except TelegramError as exc:
        log.error("review.notice_failed error=%s", type(exc).__name__)


async def reply_safely(message: Message | None, text: str, parse_mode: str | None = None,
                       reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Reply without ever raising; failures are logged."""
    if message is None:
        return
    try:
        await with_retry(lambda: message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup))
    except TelegramError as exc:
        log.error("review.reply_failed error=%s", type(exc).__name__)


async def _remove_buttons(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramError:
        pass  # old or already-edited messages can't be changed; the DB state is what matters


# --- button presses --------------------------------------------------------------------------


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Button presses. Only Meera may act; each draft can be acted on once."""
    query = update.callback_query
    if query is None:
        return
    if not is_meera(query.from_user):
        log.warning("review.unauthorised_press user_id=%s", query.from_user.id if query.from_user else None)
        await query.answer(NOT_AUTHORISED, show_alert=True)
        return
    match = CALLBACK_RE.match(query.data or "")
    if match is None:
        await query.answer("Unknown action.")
        return
    action, draft_id = match.group(1), int(match.group(2))
    # Very old messages arrive as InaccessibleMessage, which can't be replied to or edited.
    message = query.message if query.message is not None and query.message.is_accessible else None

    if action == "approve":
        await _approve(query, message, draft_id)
    elif action in ("reject", "discard"):
        await _reject(query, message, draft_id)
    elif action == "regen":
        await _regenerate(context.bot, query, message, draft_id)
    elif action == "posted":
        await _mark_posted(query, message, draft_id)
    else:
        await _start_edit(query, message, draft_id)


async def _approve(query, message: Message | None, draft_id: int) -> None:
    if not await asyncio.to_thread(db.approve_draft, draft_id):
        await query.answer(ALREADY_HANDLED)
        return
    final = await asyncio.to_thread(db.get_final_post, draft_id)
    await query.answer("Approved")
    await _remove_buttons(message)
    text, parse_mode = format_approved(final.final_body)
    await reply_safely(message, text, parse_mode, reply_markup=posted_keyboard(draft_id))
    log.info("review.approved draft_id=%s note_id=%s edited_by_meera=%s",
             draft_id, final.note_id, final.edited_by_meera)


async def _reject(query, message: Message | None, draft_id: int) -> None:
    if not await asyncio.to_thread(db.set_draft_status, draft_id, "discarded"):
        await query.answer(ALREADY_HANDLED)
        return
    d = await asyncio.to_thread(db.get_draft, draft_id)
    await asyncio.to_thread(db.set_note_status, d.note_id, "shelved")
    await query.answer("Rejected")
    await _remove_buttons(message)
    await reply_safely(message, "Rejected. Nothing was posted, and the note is shelved.")
    log.info("review.rejected draft_id=%s note_id=%s", d.id, d.note_id)


async def _mark_posted(query, message: Message | None, draft_id: int) -> None:
    if not await asyncio.to_thread(db.mark_posted, draft_id):
        await query.answer("Already marked, or not an approved draft.")
        return
    await query.answer("Marked as posted")
    await _remove_buttons(message)
    log.info("review.marked_posted draft_id=%s", draft_id)


async def _start_edit(query, message: Message | None, draft_id: int) -> None:
    if not await asyncio.to_thread(db.start_edit, draft_id):
        await query.answer(ALREADY_HANDLED)
        return
    await query.answer("Send your edit")
    await reply_safely(message, (
        f"Editing draft #{draft_id}. Reply here with either:\n"
        f"- your full rewrite (at least {config.settings.draft_min_chars} characters), which I'll keep as-is, or\n"
        "- a short instruction, e.g. 'shorter, open with the supplier call', and I'll redraft once."
    ))
    log.info("review.edit_started draft_id=%s", draft_id)


async def _regenerate(bot: Bot, query, message: Message | None, draft_id: int) -> None:
    """A fresh draft from the same note and assessment, through the same news, validators and QA."""
    d = await asyncio.to_thread(db.get_draft, draft_id)
    if d is None or d.status != "pending_review":
        await query.answer(ALREADY_HANDLED)
        return
    if await asyncio.to_thread(db.count_revisions, d.note_id) > config.settings.max_regenerations:
        await query.answer("Regeneration limit reached. Edit it or write your own version.", show_alert=True)
        return
    await query.answer("Regenerating")
    note = await asyncio.to_thread(db.get_note, d.note_id)
    assessment = await asyncio.to_thread(db.get_latest_assessment, d.note_id)
    core_idea = assessment.summary.get("core_idea", "") if assessment else ""
    try:
        result = await draft.draft_note(note.content, note.category, note.angle or "", core_idea,
                                        note.news_keywords or "")
    except gemini_client.GeminiError as exc:
        log.warning("review.regenerate_unavailable draft_id=%s error=%s", draft_id, exc)
        await reply_safely(message, "I couldn't reach the AI service, so this draft is unchanged.")
        return
    if result is None:
        await reply_safely(message, "I couldn't produce a new draft that passes the fact and voice checks, "
                                    "so this one is unchanged.")
        return
    new_id = await asyncio.to_thread(db.add_revision, draft_id, result.body, result.model, result.qa,
                                     result.news, True)
    if new_id is None:
        await reply_safely(message, ALREADY_HANDLED)
        return
    await _remove_buttons(message)
    log.info("review.regenerated draft_id=%s previous=%s", new_id, draft_id)
    await send_for_review(bot, new_id)


# --- edit replies ------------------------------------------------------------------------------


async def handle_review_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Meera's reply after pressing Edit: a full rewrite (stored verbatim) or an instruction (one redraft)."""
    message = update.message
    if message is None or message.text is None:
        return
    if not is_meera(message.from_user) or message.chat.id != config.settings.telegram_review_chat_id:
        log.warning("review.ignored_message chat_id=%s", message.chat.id)
        return
    pending = await asyncio.to_thread(db.get_awaiting_edit)
    if pending is None:
        await reply_safely(message, "No draft is waiting for an edit. Tap Edit on a draft first.")
        return

    text = message.text.strip()
    if len(text) >= config.settings.draft_min_chars:
        await _save_rewrite(context.bot, message, pending, text)
    else:
        await _redraft(context.bot, message, pending, text)


async def _save_rewrite(bot: Bot, message: Message, pending: db.Draft, text: str) -> None:
    if len(text) > config.settings.draft_max_chars:
        await reply_safely(message, f"That's {len(text)} characters; LinkedIn posts here are capped at "
                                    f"{config.settings.draft_max_chars}. Please trim it and send it again.")
        return
    # Her own words are kept exactly as written: no normalising, no model call.
    new_id = await asyncio.to_thread(db.add_revision, pending.id, text, MEERA_EDIT_MODEL)
    if new_id is None:
        await reply_safely(message, ALREADY_HANDLED)
        return
    log.info("review.rewrite_saved draft_id=%s previous=%s", new_id, pending.id)
    await send_for_review(bot, new_id)


async def _redraft(bot: Bot, message: Message, pending: db.Draft, instruction: str) -> None:
    note = await asyncio.to_thread(db.get_note, pending.note_id)
    await reply_safely(message, "Redrafting with your note...")
    try:
        revised = await draft.revise_draft(note.content, pending.body, instruction)
    except gemini_client.GeminiError as exc:
        log.warning("review.redraft_unavailable draft_id=%s error=%s", pending.id, exc)
        await reply_safely(message, "I couldn't reach the AI service, so the draft is unchanged. "
                                    "Send the instruction again later, or paste your own rewrite.")
        return
    if revised is None:
        await asyncio.to_thread(db.clear_edit, pending.id)
        await reply_safely(message, "I couldn't produce a redraft that passes the fact and voice checks, "
                                    "so the original draft is still waiting for you above.")
        return
    body, qa = revised
    new_id = await asyncio.to_thread(db.add_revision, pending.id, body, config.settings.draft_model, qa)
    if new_id is None:
        await reply_safely(message, ALREADY_HANDLED)
        return
    log.info("review.redrafted draft_id=%s previous=%s", new_id, pending.id)
    await send_for_review(bot, new_id)


async def deliver_waiting(bot: Bot) -> int:
    """Send every pending draft that never reached Meera; returns how many got through."""
    waiting = await asyncio.to_thread(db.get_undelivered_drafts)
    delivered = 0
    for d in waiting:
        delivered += await send_for_review(bot, d.id)
    return delivered


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not is_meera(message.from_user):
        return
    log.info("review.start_command")
    await reply_safely(message, "I'm set up and listening. Send a voice note to the capture channel and I'll "
                                "score it, draft it if it qualifies, and send it here for you to approve, edit, "
                                "reject or regenerate. Nothing is ever posted for you.")
    # Telegram only lets a bot message someone after they press Start, so drafts may be waiting.
    await deliver_waiting(context.bot)
