"""Score new notes for 'worth developing' and pick the best one.

The model scores; code decides. Eligibility, the threshold, the short-note filter
and tie-breaking are all enforced here, never in the prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import config
import db
import draft
import gemini_client

log = logging.getLogger(__name__)

MAX_KEYWORD_CHARS = 80
MAX_KEYWORDS = 6


@dataclass(frozen=True)
class TriageResult:
    note_id: int
    score: float
    worth_developing: bool
    category: str | None
    reason: str
    angle: str
    news_keywords: str

    def is_eligible(self, threshold: float) -> bool:
        return self.worth_developing and self.score >= threshold


def triage_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "worth_developing": {"type": "boolean"},
            "score": {"type": "number", "minimum": 0, "maximum": 10},
            "category": {"type": "string", "enum": draft.corpus_categories()},
            "reason": {"type": "string"},
            "suggested_angle": {"type": "string"},
            "news_keywords": {"type": "string"},
        },
        "required": ["worth_developing", "score", "category", "reason",
                     "suggested_angle", "news_keywords"],
    }


def build_prompt(note_text: str) -> str:
    published = "\n".join(f"- {p.title}" for p in draft.load_corpus())
    return gemini_client.render(
        gemini_client.load_prompt("triage"),
        categories=", ".join(draft.corpus_categories()),
        published=published,
        note=note_text,
    )


def clean_keywords(raw: str) -> str:
    """Model output becomes a URL query later, so keep only plain words."""
    words = re.sub(r"[^A-Za-z0-9\- ]+", " ", raw).split()
    return " ".join(words[:MAX_KEYWORDS])[:MAX_KEYWORD_CHARS].strip()


def parse_verdict(note_id: int, data: dict[str, Any]) -> TriageResult | None:
    """Validate the model's JSON in code; None means unusable (note is retried next run)."""
    try:
        score = float(data["score"])
    except (TypeError, ValueError):
        return None
    if not 0.0 <= score <= 10.0 or not isinstance(data["worth_developing"], bool):
        return None
    category = data["category"] if data["category"] in draft.corpus_categories() else None
    return TriageResult(
        note_id=note_id,
        score=score,
        # Code, not the model, has the final say: a low score is never worth developing.
        worth_developing=data["worth_developing"] and score >= config.settings.triage_threshold,
        category=category,
        reason=str(data["reason"]).strip(),
        angle=str(data["suggested_angle"]).strip(),
        news_keywords=clean_keywords(str(data["news_keywords"])),
    )


def _from_stored(note: db.Note) -> TriageResult:
    return TriageResult(note.id, note.score, bool(note.worth_developing), note.category,
                        note.triage_reason or "", note.angle or "", note.news_keywords or "")


async def _persist(result: TriageResult) -> None:
    await asyncio.to_thread(
        db.set_note_triage, result.note_id, result.score, result.worth_developing,
        result.category, result.reason, result.angle, result.news_keywords,
    )


async def score_note(note: db.Note) -> TriageResult | None:
    """Score one note (or reuse a stored score). Raises GeminiError if the model is unreachable."""
    if note.score is not None:
        return _from_stored(note)
    if len(note.content.split()) < config.settings.triage_min_words:
        result = TriageResult(note.id, 0.0, False, None, "too short to develop", "", "")
    else:
        data = await gemini_client.generate_json(
            build_prompt(note.content), triage_schema(), config.settings.triage_model
        )
        result = parse_verdict(note.id, data)
        if result is None:
            log.warning("triage.invalid_verdict note_id=%s", note.id)
            return None
    await _persist(result)
    log.info("triage.scored note_id=%s score=%.1f worth=%s category=%s",
             note.id, result.score, result.worth_developing, result.category)
    return result


async def score_notes(notes: list[db.Note]) -> list[TriageResult]:
    """Score notes oldest first; unusable verdicts are dropped, Gemini outages propagate."""
    results = []
    for note in notes:
        result = await score_note(note)
        if result is not None:
            results.append(result)
    return results


def pick_best(results: list[TriageResult], threshold: float) -> TriageResult | None:
    """Highest eligible score wins; ties go to the older note (results are oldest first)."""
    eligible = [r for r in results if r.is_eligible(threshold)]
    return max(eligible, key=lambda r: r.score) if eligible else None
