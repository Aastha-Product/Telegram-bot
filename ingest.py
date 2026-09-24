"""Capture: channel_post from the capture channel -> notes table.

Routing and storage are plain code. The only model call here is transcribing
voice notes to text; judging a note is left to triage.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from telegram import Bot, Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import config
import db
import gemini_client

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NoteInput:
    content: str
    content_type: str
    status: str
    tg_file_id: str | None = None


def extract_note(message: Message) -> NoteInput | None:
    """Turn a channel post into what we store; None means ignore it (e.g. blank text)."""
    if message.text is not None:
        text = message.text.strip()
        return NoteInput(text, "text", "new") if text else None
    if message.voice is not None:
        # Transcribed right after storing; any caption is kept alongside the transcript.
        return NoteInput((message.caption or "").strip(), "voice", "pending_transcription",
                         message.voice.file_id)
    caption = (message.caption or "").strip()
    if caption:
        return NoteInput(caption, "text", "new")
    # Stickers, bare photos, etc.: keep a record but never triage them.
    return NoteInput("", "unsupported", "shelved")


def is_capture_post(update: Update) -> bool:
    """Only new posts in the configured capture channel count as notes."""
    post = update.channel_post
    return post is not None and post.chat.id == config.settings.telegram_chat_id


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store a capture-channel post as a note. Never raises: a bad post must not stop the bot."""
    # The handler filter already restricts this, but the gate is enforced here too.
    if not is_capture_post(update):
        log.warning("ingest.rejected update_id=%s reason=not_capture_channel", update.update_id)
        return
    post = update.channel_post
    note = extract_note(post)
    if note is None:
        log.info("ingest.skipped message_id=%s reason=empty", post.message_id)
        return
    try:
        note_id = await asyncio.to_thread(
            db.add_note,
            post.message_id,
            post.chat.id,
            note.content,
            post.date,
            note.content_type,
            note.tg_file_id,
            note.status,
        )
    except Exception:
        log.exception("ingest.failed message_id=%s content_type=%s", post.message_id, note.content_type)
        return
    if note_id is None:
        log.info("ingest.duplicate message_id=%s", post.message_id)
        return
    log.info("ingest.stored note_id=%s message_id=%s content_type=%s status=%s chars=%d",
             note_id, post.message_id, note.content_type, note.status, len(note.content))
    if note.status == "pending_transcription":
        stored = await asyncio.to_thread(db.get_note, note_id)
        if stored is not None:
            await transcribe_note(context.bot, stored)


async def transcribe_note(bot: Bot, note: db.Note) -> bool:
    """Download a voice note, transcribe it, and release it to triage.

    Never raises: on failure the note stays pending_transcription and is retried later.
    """
    try:
        tg_file = await bot.get_file(note.tg_file_id)
        audio = bytes(await tg_file.download_as_bytearray())
        transcript = await gemini_client.transcribe_audio(
            audio, gemini_client.TELEGRAM_VOICE_MIME, config.settings.transcribe_model
        )
        content = f"{note.content}\n\n{transcript}" if note.content else transcript
        updated = await asyncio.to_thread(db.set_note_transcript, note.id, content)
    except (TelegramError, gemini_client.GeminiError) as exc:
        log.warning("ingest.transcribe_failed note_id=%s error=%s", note.id, exc)
        return False
    except Exception:
        log.exception("ingest.transcribe_failed note_id=%s", note.id)
        return False
    if updated:
        log.info("ingest.transcribed note_id=%s chars=%d", note.id, len(content))
    return updated


async def transcribe_pending(bot: Bot) -> int:
    """Retry every voice note still waiting for a transcript; return how many succeeded."""
    try:
        pending = await asyncio.to_thread(db.get_pending_transcriptions)
    except Exception:
        log.exception("ingest.transcribe_pending_failed")
        return 0
    done = 0
    for note in pending:
        done += await transcribe_note(bot, note)
    if pending:
        log.info("ingest.transcribe_pending attempted=%d succeeded=%d", len(pending), done)
    return done
