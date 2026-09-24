"""Thin wrapper around google-genai: JSON schema output, retries, repair. All model calls go through here."""

from __future__ import annotations

# TODO(Phase 4): transcribe_audio(audio_bytes, mime_type, model) -> str;
# generate_json(prompt, schema, model) -> dict; GeminiError.
