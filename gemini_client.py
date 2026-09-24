"""Thin wrapper around google-genai: JSON schema output, retries, repair. All model calls go through here."""

from __future__ import annotations

# TODO(Phase 4): generate_json(prompt, schema, model) -> dict; GeminiError.
