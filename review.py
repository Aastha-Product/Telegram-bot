"""Review gate: send drafts to Meera with Approve/Edit/Discard and handle her presses.

This is the terminal step. Nothing here, or anywhere else, publishes: Approve only
hands Meera clean text to post on LinkedIn herself.
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

log = logging.getLogger(__name__)

T = TypeVar("T")

CALLBACK_RE = re.compile(r"^(approve|edit|discard):(\d{1,12})$")
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
TELEGRAM_TEXT_LIMIT = 4096
MEERA_EDIT_MODEL = "meera-edit"

NOT_AUTHORISED = "Only Meera can review drafts."
ALREADY_HANDLED = "This draft was already handled."


def is_meera(user: User | None) -> bool:
    """Authorisation is always checked server-side against the configured id."""
    return user is not None and user.id == config.settings.meera_user_id


def review_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=f"approve:{draft_id}"),
        InlineKeyboardButton("Edit", callback_data=f"edit:{draft_id}"),
        InlineKeyboardButton("Discard", callback_data=f"discard:{draft_id}"),
    ]])


def format_for_review(d: db.Draft, note: db.Note | None) -> str:
    meta = [f"Draft #{d.id}", f"rev {d.revision}"]
    if note is not None and note.category:
        meta.append(note.category)
    if note is not None and note.score is not None:
        meta.append(f"score {note.score:g}/10")
    parts = [" · ".join(meta), d.body]
    if d.source_url:
        parts.append(f"News source used: {d.source_url}")
    parts.append("Nothing is posted anywhere unless you post it yourself.")
    return "\n\n".join(parts)


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
    """Deliver a draft with buttons; False leaves it undelivered so the next run retries."""
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


async def _reply(message: Message | None, text: str, parse_mode: str | None = None) -> None:
    if message is None:
        return
    try:
        await with_retry(lambda: message.reply_text(text, parse_mode=parse_mode))
    except TelegramError as exc:
        log.error("review.reply_failed error=%s", type(exc).__name__)


async def _remove_buttons(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramError:
        pass  # old or already-edited messages can't be changed; the DB state is what matters


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve / Edit / Discard presses. Only Meera may act; each draft can be acted on once."""
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
    elif action == "discard":
        await _discard(query, message, draft_id)
    else:
        await _start_edit(query, message, draft_id)


async def _approve(query, message: Message | None, draft_id: int) -> None:
    if not await asyncio.to_thread(db.set_draft_status, draft_id, "approved"):
        await query.answer(ALREADY_HANDLED)
        return
    d = await asyncio.to_thread(db.get_draft, draft_id)
    await query.answer("Approved")
    await _remove_buttons(message)
    text, parse_mode = format_approved(d.body)
    await _reply(message, text, parse_mode)
    log.info("review.approved draft_id=%s note_id=%s", d.id, d.note_id)


async def _discard(query, message: Message | None, draft_id: int) -> None:
    if not await asyncio.to_thread(db.set_draft_status, draft_id, "discarded"):
        await query.answer(ALREADY_HANDLED)
        return
    d = await asyncio.to_thread(db.get_draft, draft_id)
    await asyncio.to_thread(db.set_note_status, d.note_id, "shelved")
    await query.answer("Discarded")
    await _remove_buttons(message)
    await _reply(message, "Discarded. Nothing was posted, and the note is shelved.")
    log.info("review.discarded draft_id=%s note_id=%s", d.id, d.note_id)


async def _start_edit(query, message: Message | None, draft_id: int) -> None:
    if not await asyncio.to_thread(db.start_edit, draft_id):
        await query.answer(ALREADY_HANDLED)
        return
    await query.answer("Send your edit")
    await _reply(message, (
        f"Editing draft #{draft_id}. Reply here with either:\n"
        f"- your full rewrite (at least {config.settings.draft_min_chars} characters), which I'll keep as-is, or\n"
        "- a short instruction, e.g. 'shorter, open with the supplier call', and I'll redraft once."
    ))
    log.info("review.edit_started draft_id=%s", draft_id)


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
        await _reply(message, "No draft is waiting for an edit. Tap Edit on a draft first.")
        return

    text = message.text.strip()
    if len(text) >= config.settings.draft_min_chars:
        await _save_rewrite(context.bot, message, pending, text)
    else:
        await _redraft(context.bot, message, pending, text)


async def _save_rewrite(bot: Bot, message: Message, pending: db.Draft, text: str) -> None:
    if len(text) > config.settings.draft_max_chars:
        await _reply(message, f"That's {len(text)} characters; LinkedIn posts here are capped at "
                              f"{config.settings.draft_max_chars}. Please trim it and send it again.")
        return
    # Her own words are kept exactly as written: no normalising, no model call.
    new_id = await asyncio.to_thread(db.add_revision, pending.id, text, MEERA_EDIT_MODEL)
    if new_id is None:
        await _reply(message, ALREADY_HANDLED)
        return
    log.info("review.rewrite_saved draft_id=%s previous=%s", new_id, pending.id)
    await send_for_review(bot, new_id)


async def _redraft(bot: Bot, message: Message, pending: db.Draft, instruction: str) -> None:
    note = await asyncio.to_thread(db.get_note, pending.note_id)
    await _reply(message, "Redrafting with your note...")
    try:
        body = await draft.revise_draft(note.content, pending.body, instruction)
    except gemini_client.GeminiError as exc:
        log.warning("review.redraft_unavailable draft_id=%s error=%s", pending.id, exc)
        await _reply(message, "I couldn't reach the AI service, so the draft is unchanged. "
                              "Send the instruction again later, or paste your own rewrite.")
        return
    if body is None:
        await asyncio.to_thread(db.clear_edit, pending.id)
        await _reply(message, "I couldn't produce a redraft that passes the fact and voice checks, "
                              "so the original draft is still waiting for you above.")
        return
    new_id = await asyncio.to_thread(db.add_revision, pending.id, body, config.settings.draft_model)
    if new_id is None:
        await _reply(message, ALREADY_HANDLED)
        return
    log.info("review.redrafted draft_id=%s previous=%s", new_id, pending.id)
    await send_for_review(bot, new_id)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not is_meera(message.from_user):
        return
    await _reply(message, "I'm set up and listening. Drafts will arrive in this chat for you to "
                          "approve, edit or discard. Nothing is ever posted for you.")
