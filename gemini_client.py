"""Thin wrapper around google-genai: JSON schema output, retries, repair. All model calls go through here.

Uses the SDK's async client so model calls never block the bot's event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

import config

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0
REQUEST_TIMEOUT_MS = 120_000
TELEGRAM_VOICE_MIME = "audio/ogg"

# We never give the model tools, so automatic function calling stays off.
_NO_TOOLS = types.AutomaticFunctionCallingConfig(disable=True)

_client: genai.Client | None = None


class GeminiError(Exception):
    """A model call failed for good (after retries/repair). Message never contains the API key."""


CLARITY_LEVELS: tuple[str, ...] = ("high", "medium", "low")
TRANSCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "clarity": {"type": "string", "enum": list(CLARITY_LEVELS)},
        "languages": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["transcript", "clarity", "languages"],
}


@dataclass(frozen=True)
class Transcript:
    text: str
    clarity: str  # the model's own estimate; Gemini reports no numeric confidence
    languages: list[str]


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def render(template: str, **values: str) -> str:
    """Fill {name} placeholders without str.format, so literal braces in prompts are safe."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        # SDK-level retries stay off (its default); _generate owns the retry policy.
        _client = genai.Client(
            api_key=config.settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )
    return _client


def _retry_after_seconds(exc: errors.APIError) -> float | None:
    headers = getattr(exc.response, "headers", None) or {}
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError):
        return exc.code == 429
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _describe(exc: Exception) -> str:
    """Short, key-free description for logs and GeminiError messages."""
    if isinstance(exc, errors.APIError):
        return f"{type(exc).__name__} {exc.code} {exc.status}"
    return type(exc).__name__


async def _generate(model: str, contents: Any, gen_config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    """One logical call: retry 5xx/429/timeouts with exponential backoff, honouring retry-after."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await _get_client().aio.models.generate_content(
                model=model, contents=contents, config=gen_config
            )
        except Exception as exc:
            if not _is_retryable(exc):
                raise GeminiError(f"model call failed: {_describe(exc)}") from None
            if attempt == MAX_RETRIES:
                raise GeminiError(f"model call failed after {MAX_RETRIES} retries: {_describe(exc)}") from None
            delay = BACKOFF_BASE_SECONDS * 2**attempt
            if isinstance(exc, errors.APIError):
                delay = _retry_after_seconds(exc) or delay
            delay = min(delay, MAX_BACKOFF_SECONDS)
            log.warning("gemini.retry model=%s attempt=%d error=%s wait_s=%.1f",
                        model, attempt + 1, _describe(exc), delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _parse_json(text: str | None, schema: dict[str, Any]) -> dict[str, Any] | None:
    """Return the object if it is JSON with every required key, else None."""
    try:
        data = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if any(key not in data for key in schema.get("required", [])):
        return None
    return data


async def generate_json(prompt: str, schema: dict[str, Any], model: str,
                        attachments: list[types.Part] | None = None) -> dict[str, Any]:
    """Ask for JSON matching `schema`; one repair re-ask if the reply is unusable."""
    gen_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=schema,
        automatic_function_calling=_NO_TOOLS,
    )
    contents: Any = [prompt, *attachments] if attachments else prompt
    response = await _generate(model, contents, gen_config)
    data = _parse_json(response.text, schema)
    if data is not None:
        return data

    log.warning("gemini.invalid_json model=%s action=repair", model)
    repair_prompt = render(load_prompt("repair"), schema=json.dumps(schema, indent=2),
                           previous=response.text or "")
    response = await _generate(model, repair_prompt, gen_config)
    data = _parse_json(response.text, schema)
    if data is None:
        raise GeminiError("model returned invalid JSON after one repair attempt")
    return data


async def transcribe_audio(audio: bytes, mime_type: str, model: str) -> Transcript:
    """Verbatim transcript plus the model's clarity estimate. Raises GeminiError if no speech came back."""
    if not audio:
        raise GeminiError("no audio to transcribe")
    data = await generate_json(load_prompt("transcribe"), TRANSCRIPT_SCHEMA, model,
                               attachments=[types.Part.from_bytes(data=audio, mime_type=mime_type)])
    text = str(data["transcript"]).strip()
    if not text:
        raise GeminiError("transcription came back empty")
    clarity = data["clarity"] if data["clarity"] in CLARITY_LEVELS else "low"  # unknown means untrusted
    languages = [str(x) for x in data["languages"]] if isinstance(data["languages"], list) else []
    return Transcript(text=text, clarity=clarity, languages=languages)
