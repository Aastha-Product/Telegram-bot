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
import review

NOTICE_UNINTELLIGIBLE = ("I couldn't make out your latest voice note (it may be silent or too noisy), "
                         "so nothing was saved from it. Please re-record it or type it.")

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


def sender_of(post: Message) -> str:
    """Who posted: channel posts usually carry a signature or the channel itself, not a user."""
    if post.from_user is not None:
        return f"user:{post.from_user.id}"
    if post.author_signature:
        return f"signature:{post.author_signature}"
    if post.sender_chat is not None:
        return f"chat:{post.sender_chat.id}"
    return "unknown"


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Store a capture-channel post as a note; return its id if it is ready for triage.

    Never raises: a bad post must not stop the bot.
    """
    # The handler filter already restricts this, but the gate is enforced here too.
    if not is_capture_post(update):
        log.warning("ingest.rejected update_id=%s reason=not_capture_channel", update.update_id)
        return None
    post = update.channel_post
    note = extract_note(post)
    if note is None:
        log.info("ingest.skipped message_id=%s reason=empty", post.message_id)
        return None
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
            sender_of(post),
        )
    except Exception:
        log.exception("ingest.failed message_id=%s content_type=%s", post.message_id, note.content_type)
        return None
    if note_id is None:
        log.info("ingest.duplicate message_id=%s", post.message_id)
        return None
    log.info("ingest.stored note_id=%s message_id=%s content_type=%s status=%s chars=%d",
             note_id, post.message_id, note.content_type, note.status, len(note.content))
    if note.status == "new":
        return note_id
    if note.status == "pending_transcription":
        stored = await asyncio.to_thread(db.get_note, note_id)
        if stored is not None and await transcribe_note(context.bot, stored):
            return note_id
    return None


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
        ratio = transcript.unclear_ratio
        content = f"{note.content}\n\n{transcript.text}" if note.content else transcript.text
        updated = await asyncio.to_thread(db.set_note_transcript, note.id, content, transcript.clarity, ratio)
    except gemini_client.EmptyTranscript:
        # Silence or noise: retrying the same audio won't help, so shelve it and tell Meera.
        await asyncio.to_thread(db.set_note_status, note.id, "shelved")
        await review.send_notice(bot, NOTICE_UNINTELLIGIBLE)
        log.info("ingest.unintelligible note_id=%s action=shelved", note.id)
        return False
    except (TelegramError, gemini_client.GeminiError) as exc:
        log.warning("ingest.transcribe_failed note_id=%s error=%s", note.id, exc)
        return False
    except Exception:
        log.exception("ingest.transcribe_failed note_id=%s", note.id)
        return False
    if updated:
        log.info("ingest.transcribed note_id=%s chars=%d clarity=%s unclear_ratio=%.3f languages=%s",
                 note.id, len(content), transcript.clarity, ratio, ",".join(transcript.languages))
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
