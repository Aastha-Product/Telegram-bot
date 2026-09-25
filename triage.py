"""Publishability triage: 10 scored parameters, guardrails, and a decision (docs/publishability_rubric.md).

The model scores and quotes evidence; code verifies the evidence against the transcript,
applies guardrail caps, computes the weighted overall score, and decides. Nothing that
decides eligibility lives in the prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import config
import db
import draft
import gemini_client
import news

log = logging.getLogger(__name__)

RUBRIC_VERSION = "v2"
MAX_KEYWORD_CHARS = 80
MAX_KEYWORDS = 4
EVIDENCE_MIN_WORDS = 3
EVIDENCE_MIN_COVERAGE = 0.8
EVIDENCE_CAP = 5.0
GUARDRAIL_CAPS: dict[str, float] = {"pass": 10.0, "warn": 6.0, "fail": 2.0}
IMPROVEMENT_TIPS = 3

QUALIFIED, REJECTED, HUMAN_REVIEW = "qualified", "rejected", "human_review"


@dataclass(frozen=True)
class Parameter:
    key: str
    name: str
    weight: float
    question: str
    guardrail: str


PARAMETERS: tuple[Parameter, ...] = (
    Parameter("founder_relevance", "Founder Relevance", 1.0,
              "Does it come from Meera's own role, formulation expertise, business or founder journey?",
              "fail if the topic is outside her expertise but would be presented as authority"),
    Parameter("audience_relevance", "Audience Relevance", 1.0,
              "Does it matter to her audience: urban Indian women buying skincare, and industry peers?",
              "warn if it only matters to a niche with no link to her audience"),
    Parameter("originality", "Originality", 1.5,
              "Is there a non-generic observation or angle that is not a repeat of her published pieces?",
              "fail if it restates a published piece with nothing new, or is generic advice"),
    Parameter("insight_depth", "Insight Depth", 1.0,
              "Is there a mechanism or a 'why', not just a statement?",
              "warn if it stays surface-level"),
    Parameter("authenticity", "Authenticity", 1.5,
              "Is this something Meera experienced, observed, tested or believes?",
              "fail if it cannot reasonably be attributed to Meera"),
    Parameter("practical_value", "Practical Value", 1.0,
              "Does the reader get a lesson, check, principle or takeaway?",
              "warn if there is no takeaway"),
    Parameter("credibility", "Credibility", 1.5,
              "Is it supported by her experience, evidence or reasoning?",
              "warn for sweeping claims without support; fail for unsupported factual or statistical claims"),
    Parameter("linkedin_fit", "LinkedIn Fit", 0.5,
              "Would it naturally become a strong founder-led LinkedIn post?",
              "warn if it would only work as bait or controversy"),
    Parameter("discussion_potential", "Discussion Potential", 0.5,
              "Could it start thoughtful professional discussion without engagement bait?",
              "fail if the discussion would depend on attacking someone"),
    Parameter("postability", "Postability", 1.5,
              "Could it become a strong standalone post WITHOUT inventing missing facts, stories or numbers?",
              "fail if a post would need invented material"),
)
PARAMETER_KEYS: tuple[str, ...] = tuple(p.key for p in PARAMETERS)

MODEL_FLAG_TYPES: tuple[str, ...] = (
    "fabricated_information", "unsupported_claim", "fake_statistic", "invented_experience",
    "confidential_information", "private_customer_information", "sensitive_personal_information",
    "defamatory", "unverified_accusation", "misleading", "plagiarism", "not_attributable", "not_for_public",
)

# Deterministic personal-data detectors (code-side hard guardrail, independent of the model).
PERSONAL_DATA_PATTERNS: dict[str, re.Pattern[str]] = {
    "email address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone number": re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),
    "ID-like number": re.compile(r"(?<![\d.])\d{4}[\s-]\d{4}[\s-]\d{4}(?![\d.])|(?<![\d.])\d{12,}(?![\d.])"),
}


@dataclass(frozen=True)
class ParameterScore:
    key: str
    name: str
    weight: float
    raw_score: float
    score: float
    reason: str
    evidence: str
    evidence_verified: bool
    gap: str
    guardrail: str
    guardrail_note: str
    caps: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriageResult:
    note_id: int
    decision: str
    overall: float
    parameters: tuple[ParameterScore, ...]
    hard_flags: tuple[dict[str, str], ...]
    summary: dict[str, str]
    category: str | None
    angle: str
    news_keywords: str
    model: str
    improvements: tuple[str, ...] = field(default=())

    @property
    def qualified(self) -> bool:
        return self.decision == QUALIFIED


# --- schema & prompt ----------------------------------------------------------------


def triage_schema() -> dict[str, Any]:
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "summary": {
                "type": "object",
                "properties": {k: text for k in ("core_idea", "founder_perspective", "intended_audience",
                                                 "main_insight", "topic")},
                "required": ["core_idea", "founder_perspective", "intended_audience", "main_insight", "topic"],
            },
            "parameters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": list(PARAMETER_KEYS)},
                        "score": {"type": "number", "minimum": 0, "maximum": 10},
                        "reason": text, "evidence": text, "gap": text,
                        "guardrail": {"type": "string", "enum": list(GUARDRAIL_CAPS)},
                        "guardrail_note": text,
                    },
                    "required": ["key", "score", "reason", "evidence", "gap", "guardrail", "guardrail_note"],
                },
            },
            "hard_flags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"type": {"type": "string", "enum": list(MODEL_FLAG_TYPES)},
                                   "detail": text, "quote": text},
                    "required": ["type", "detail", "quote"],
                },
            },
            "category": {"type": "string", "enum": draft.corpus_categories()},
            "suggested_angle": text,
            "news_keywords": text,
        },
        "required": ["summary", "parameters", "hard_flags", "category", "suggested_angle", "news_keywords"],
    }


def build_prompt(note_text: str) -> str:
    rubric = "\n".join(f"- {p.key} ({p.name}): {p.question} Guardrail: {p.guardrail}." for p in PARAMETERS)
    return gemini_client.render(
        gemini_client.load_prompt("triage"),
        parameters=rubric,
        flag_types=", ".join(MODEL_FLAG_TYPES),
        categories=", ".join(draft.corpus_categories()),
        published="\n".join(f"- {p.title}" for p in draft.load_corpus()),
        note=note_text,
    )


# --- deterministic checks -------------------------------------------------------------


def is_low_confidence(note: db.Note) -> bool:
    """A voice transcript we can't trust must not be scored or drafted from."""
    if note.content_type != "voice":
        return False
    ratio = note.unclear_ratio if note.unclear_ratio is not None else 0.0
    return note.transcript_clarity == "low" or ratio > config.settings.transcript_max_unclear_ratio


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def evidence_is_verified(quote: str, transcript: str) -> bool:
    """The quote must really come from the transcript: >=80% of its words present (min 3 words)."""
    words = _tokens(quote)
    if len(words) < EVIDENCE_MIN_WORDS:
        return False
    available = set(_tokens(transcript))
    return sum(w in available for w in words) / len(words) >= EVIDENCE_MIN_COVERAGE


def personal_data_flags(transcript: str) -> list[dict[str, str]]:
    return [{"type": "personal_data_detected", "detail": f"contains an {label}" if label[0] in "aeiouAEIOU"
             else f"contains a {label}", "quote": match.group(0), "source": "code"}
            for label, pattern in PERSONAL_DATA_PATTERNS.items() if (match := pattern.search(transcript))]


def weighted_overall(parameters: tuple[ParameterScore, ...]) -> float:
    """Σ(weight × score) ÷ Σ(weight), rounded half-up to one decimal: reproducible from the scorecard."""
    total = sum(Decimal(str(p.weight)) * Decimal(str(p.score)) for p in parameters)
    weights = sum(Decimal(str(p.weight)) for p in parameters)
    return float((total / weights).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def decide(overall: float, hard_flags: tuple[dict[str, str], ...] | list[dict[str, str]]) -> str:
    """Guardrail failure overrides the score; otherwise strictly above the threshold qualifies."""
    if hard_flags:
        return HUMAN_REVIEW
    return QUALIFIED if overall > config.settings.triage_threshold else REJECTED


def improvement_tips(parameters: tuple[ParameterScore, ...]) -> tuple[str, ...]:
    weakest = sorted(parameters, key=lambda p: (p.score, -p.weight))[:IMPROVEMENT_TIPS]
    return tuple(f"{p.name}: {p.gap}" for p in weakest if p.gap)


def clean_keywords(raw: str) -> str:
    """Model output becomes a URL query later, so keep only plain words."""
    words = re.sub(r"[^A-Za-z0-9\- ]+", " ", raw).split()
    return " ".join(words[:MAX_KEYWORDS])[:MAX_KEYWORD_CHARS].strip()


def score_parameter(item: dict[str, Any], transcript: str) -> ParameterScore:
    spec = next(p for p in PARAMETERS if p.key == item["key"])
    raw = float(item["score"])
    guardrail = item["guardrail"]
    evidence = str(item["evidence"]).strip()
    verified = evidence_is_verified(evidence, transcript)
    score, caps = raw, []
    if not verified and score > EVIDENCE_CAP:
        score, caps = EVIDENCE_CAP, caps + ["evidence not found in transcript"]
    if score > GUARDRAIL_CAPS[guardrail]:
        score, caps = GUARDRAIL_CAPS[guardrail], caps + [f"guardrail {guardrail}"]
    return ParameterScore(spec.key, spec.name, spec.weight, raw, score, str(item["reason"]).strip(), evidence,
                          verified, str(item["gap"]).strip(), guardrail, str(item["guardrail_note"]).strip(),
                          tuple(caps))


def parse_assessment(note: db.Note, data: dict[str, Any], model: str) -> TriageResult | None:
    """Validate the model's JSON in code; None means unusable (retried later)."""
    items = data.get("parameters")
    if not isinstance(items, list):
        return None
    try:
        by_key = {item["key"]: item for item in items}
        if sorted(by_key) != sorted(PARAMETER_KEYS) or len(items) != len(PARAMETER_KEYS):
            return None
        for item in items:
            if not 0.0 <= float(item["score"]) <= 10.0 or item["guardrail"] not in GUARDRAIL_CAPS:
                return None
        parameters = tuple(score_parameter(by_key[k], note.content) for k in PARAMETER_KEYS)
        flags = [{"type": str(f["type"]), "detail": str(f["detail"]), "quote": str(f["quote"]), "source": "model"}
                 for f in data.get("hard_flags") or [] if f.get("type") in MODEL_FLAG_TYPES]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    flags += personal_data_flags(note.content)
    overall = weighted_overall(parameters)
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    category = data.get("category") if data.get("category") in draft.corpus_categories() else None
    return TriageResult(
        note_id=note.id, decision=decide(overall, flags), overall=overall, parameters=parameters,
        hard_flags=tuple(flags), summary={k: str(v) for k, v in summary.items()}, category=category,
        angle=str(data.get("suggested_angle", "")).strip(),
        news_keywords=clean_keywords(str(data.get("news_keywords", ""))),
        model=model, improvements=improvement_tips(parameters),
    )


def code_only_result(note: db.Note, decision: str, reason: str, flags: list[dict[str, str]]) -> TriageResult:
    """Verdicts code can reach without a model call (too short, untrustworthy transcript)."""
    parameters = tuple(ParameterScore(p.key, p.name, p.weight, 0.0, 0.0, reason, "", False, reason, "pass", "")
                       for p in PARAMETERS)
    return TriageResult(note.id, decision, 0.0, parameters, tuple(flags), {"core_idea": reason}, None, "", "",
                        model="code", improvements=(reason,))


# --- persistence ----------------------------------------------------------------------


def _as_row(p: ParameterScore) -> dict[str, Any]:
    return {"key": p.key, "name": p.name, "weight": p.weight, "raw_score": p.raw_score, "score": p.score,
            "reason": p.reason, "evidence": p.evidence, "evidence_verified": p.evidence_verified, "gap": p.gap,
            "guardrail": p.guardrail, "guardrail_note": p.guardrail_note, "caps": list(p.caps)}


def _from_stored(note: db.Note, a: db.Assessment) -> TriageResult:
    parameters = tuple(ParameterScore(
        p["key"], p["name"], p["weight"], p["raw_score"], p["score"], p["reason"], p["evidence"],
        p["evidence_verified"], p["gap"], p["guardrail"], p["guardrail_note"], tuple(p["caps"]))
        for p in a.parameters)
    return TriageResult(note.id, a.decision, a.overall, parameters, tuple(a.hard_flags), a.summary,
                        note.category, note.angle or "", note.news_keywords or "", a.model,
                        improvement_tips(parameters))


async def _persist(result: TriageResult) -> int:
    assessment_id = await asyncio.to_thread(
        db.add_assessment, result.note_id, result.model, RUBRIC_VERSION, result.summary.get("topic"),
        result.summary, [_as_row(p) for p in result.parameters], list(result.hard_flags), result.overall,
        result.decision,
    )
    await asyncio.to_thread(db.set_note_triage, result.note_id, result.overall, result.qualified,
                            result.category, result.summary.get("main_insight", ""), result.angle,
                            result.news_keywords)
    return assessment_id


# --- entry point -------------------------------------------------------------------------


async def assess_note(note: db.Note) -> TriageResult | None:
    """Assess one note (or reuse its stored assessment).

    Returns None if the model's answer was unusable twice (the note is retried later).
    Raises GeminiError if the model is unreachable.
    """
    stored = await asyncio.to_thread(db.get_latest_assessment, note.id)
    if stored is not None:
        return _from_stored(note, stored)

    if is_low_confidence(note):
        result = code_only_result(note, HUMAN_REVIEW, "the voice transcript is not clear enough to trust",
                                  [{"type": "low_confidence_transcript",
                                    "detail": f"clarity={note.transcript_clarity}, unclear share={note.unclear_ratio}",
                                    "quote": "", "source": "code"}])
    elif len(note.content.split()) < config.settings.triage_min_words:
        result = code_only_result(note, REJECTED, "too short to develop into a post", [])
    else:
        data = await gemini_client.generate_json(build_prompt(note.content), triage_schema(),
                                                 config.settings.triage_model)
        result = parse_assessment(note, data, config.settings.triage_model)
        if result is None:
            log.warning("triage.invalid_assessment note_id=%s", note.id)
            return None
    await _persist(result)
    log.info("triage.assessed note_id=%s overall=%.1f decision=%s flags=%d capped=%d",
             note.id, result.overall, result.decision, len(result.hard_flags),
             sum(1 for p in result.parameters if p.caps))
    return result


# --- topic suggestions for notes that don't qualify ------------------------------------------

SUGGESTION_COUNT = 3
# Searches across Meera's content areas, used to ground suggestions in current industry news.
SUGGESTION_NEWS_QUERIES: tuple[str, ...] = (
    "skincare formulation India",
    "cosmetic regulation India",
    "skincare ingredient safety",
    "sunscreen India",
)


def suggestion_schema() -> dict[str, Any]:
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"topic": text, "why_it_fits": text, "question": text,
                                   "category": {"type": "string", "enum": draft.corpus_categories()},
                                   "news_url": text},
                    "required": ["topic", "why_it_fits", "question", "category", "news_url"],
                },
            },
        },
        "required": ["suggestions"],
    }


async def current_headlines() -> list[news.NewsItem]:
    """A handful of recent, credible headlines across her content areas; [] if news is unavailable."""
    seen: dict[str, news.NewsItem] = {}
    for query in SUGGESTION_NEWS_QUERIES:
        for item in await news.fetch_news(query):
            seen.setdefault(item.url, item)
    return list(seen.values())


def parse_suggestions(data: dict[str, Any], headlines: list[news.NewsItem]) -> list[dict[str, str]]:
    """Validate in code: complete items only, at most three, and news only if it was actually fetched."""
    by_url = {h.url: h for h in headlines}
    categories = draft.corpus_categories()
    parsed = []
    for item in data.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        topic, question = str(item.get("topic", "")).strip(), str(item.get("question", "")).strip()
        if not topic or not question:
            continue
        headline = by_url.get(str(item.get("news_url", "")).strip())
        parsed.append({
            "topic": topic,
            "why_it_fits": str(item.get("why_it_fits", "")).strip(),
            "question": question,
            "category": item.get("category") if item.get("category") in categories else "",
            "news_headline": headline.title if headline else "",
            "news_source": headline.source if headline else "",
            "news_url": headline.url if headline else "",
        })
    return parsed[:SUGGESTION_COUNT]


async def suggest_topics(result: TriageResult) -> list[dict[str, str]]:
    """Topics that would suit Meera better. Enrichment only: returns [] on any failure, never raises."""
    try:
        headlines = await current_headlines()
        weaknesses = "; ".join(result.improvements) or "not enough original, first-hand material"
        prompt = gemini_client.render(
            gemini_client.load_prompt("suggest"),
            core_idea=result.summary.get("core_idea", "") or "unclear",
            weaknesses=weaknesses,
            categories=", ".join(draft.corpus_categories()),
            published="\n".join(f"- {p.title}" for p in draft.load_corpus()),
            news="\n".join(f"- {h.title} | {h.source} | {h.url}" for h in headlines) or "None available.",
            count=str(SUGGESTION_COUNT),
        )
        data = await gemini_client.generate_json(prompt, suggestion_schema(), config.settings.triage_model)
        return parse_suggestions(data, headlines)
    except Exception as exc:
        log.warning("triage.suggestions_unavailable note_id=%s error=%s", result.note_id, type(exc).__name__)
        return []
