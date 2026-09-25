"""Voice-matched drafting from a note plus corpus exemplars, with code-side validation.

The model writes; this module decides whether what it wrote may reach Meera.
Anything that fails validation gets one redraft, then is dropped (fail closed).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import config
import gemini_client
import news as news_source
from news import NewsItem

log = logging.getLogger(__name__)

CORPUS_DIR = Path(__file__).parent / "corpus"
EXEMPLAR_COUNT = 3


@dataclass(frozen=True)
class CorpusPiece:
    id: int
    source_id: str
    format: str  # "linkedin" | "newsletter"
    category: str
    title: str
    body: str


@dataclass(frozen=True)
class DraftResult:
    body: str
    source_url: str | None
    exemplar_ids: list[int]
    model: str
    news: dict[str, str] | None = None  # headline/source/date/url/relevance of the cited hook
    qa: dict[str, Any] | None = None  # the second-pass fact check that this draft passed


# --- corpus ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_corpus() -> tuple[CorpusPiece, ...]:
    """Meera's published pieces: the only voice reference we have. Loaded once."""
    index = json.loads((CORPUS_DIR / "corpus_index.json").read_text(encoding="utf-8"))
    pieces = []
    for key, meta in index.items():
        body = (CORPUS_DIR / meta["path"]).read_text(encoding="utf-8").strip()
        if not body:
            raise ValueError(f"corpus piece {key} is empty")
        pieces.append(CorpusPiece(int(key), meta["source_id"], meta["format"],
                                  meta["category"], meta["title"], body))
    if not pieces:
        raise ValueError("corpus is empty")
    return tuple(sorted(pieces, key=lambda p: p.id))


def corpus_categories() -> list[str]:
    return sorted({p.category for p in load_corpus()})


def pick_exemplars(category: str | None, n: int = EXEMPLAR_COUNT) -> list[CorpusPiece]:
    """Same-category pieces first, then LinkedIn posts; always at least one LinkedIn post for format."""
    corpus = load_corpus()
    linkedin_first = sorted(corpus, key=lambda p: (p.format != "linkedin", p.id))
    ordered = [p for p in linkedin_first if p.category == category]
    ordered += [p for p in linkedin_first if p not in ordered]
    picked = ordered[:n]
    if picked and not any(p.format == "linkedin" for p in picked):
        picked[-1] = next(p for p in linkedin_first if p.format == "linkedin")
    return picked


def format_exemplar(piece: CorpusPiece) -> str:
    """Newsletter greetings and sign-offs aren't LinkedIn conventions, so they're dropped."""
    lines = piece.body.splitlines()
    if lines and lines[0].strip() == "Hi,":
        lines = lines[1:]
    if lines and lines[-1].strip() == "Meera":
        lines = lines[:-1]
    body = "\n".join(lines).strip()
    return f"--- Example ({piece.format}, {piece.category}) ---\n{body}"


def format_news(items: list[NewsItem]) -> str:
    if not items:
        return "None available. Do not mention any news."
    return "\n".join(
        f"[{n}] {i.title} | {i.source} | {i.published:%d %b %Y} | {i.url}" if i.published
        else f"[{n}] {i.title} | {i.source} | {i.url}"
        for n, i in enumerate(items, start=1)
    )


# --- normalisation (safe, deterministic fixes) ---------------------------------------

BRITISH_SPELLINGS: dict[str, str] = {
    "moisturizer": "moisturiser", "moisturizers": "moisturisers", "moisturizing": "moisturising",
    "color": "colour", "colors": "colours", "behavior": "behaviour", "behaviors": "behaviours",
    "flavor": "flavour", "favorite": "favourite", "labeling": "labelling", "labeled": "labelled",
    "organization": "organisation", "organizations": "organisations", "optimize": "optimise",
    "optimized": "optimised", "recognize": "recognise", "realize": "realise", "realized": "realised",
    "analyze": "analyse", "analyzed": "analysed", "minimize": "minimise", "maximize": "maximise",
    "stabilize": "stabilise", "stabilized": "stabilised", "oxidize": "oxidise", "oxidized": "oxidised",
    "oxidizes": "oxidises", "sensitization": "sensitisation", "sensitize": "sensitise",
    "standardized": "standardised", "center": "centre", "fiber": "fibre", "emphasize": "emphasise",
    "prioritize": "prioritise", "apologize": "apologise", "summarize": "summarise",
}
_SPELLING_RE = re.compile(r"\b(" + "|".join(BRITISH_SPELLINGS) + r")\b", re.IGNORECASE)


def _british(match: re.Match[str]) -> str:
    word = match.group(0)
    fixed = BRITISH_SPELLINGS[word.lower()]
    return fixed.capitalize() if word[0].isupper() else fixed


def normalise(body: str) -> str:
    body = body.replace("\r\n", "\n").replace("**", "")
    body = re.sub(r"[ \t]*[—–][ \t]*", " - ", body)  # her dashes are spaced hyphens
    body = _SPELLING_RE.sub(_british, body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


# --- validation (the safety spine: PLAN.md §10.3) -----------------------------------

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
HASHTAG_RE = re.compile(r"(?<![\w&])#[A-Za-z]\w*")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
STUDY_RE = re.compile(
    r"\b(?:a|an|one|recent|new|landmark|\d{4})\s+(?:\w+\s+){0,2}(study|survey|trial|paper|report|analysis)\b"
    r"|\b(research|researchers|scientists|experts|data)\s+(?:found|find|finds|show|shows|showed|suggest|suggests|prove|proves)\b"
    r"|\b(according to)\b",
    re.IGNORECASE,
)
CTA_RE = re.compile(
    r"\b(dm me|comment below|drop a comment|link in (?:my )?bio|follow (?:me|for)|let me know|"
    r"what do you think|thoughts\?|agree\?|share this|share if|like and share|shop now|use code|sign up)",
    re.IGNORECASE,
)
CLICHE_RE = re.compile(
    r"\b(here'?s the thing|let that sink in|unpopular opinion|hot take|game[- ]changer|holy grail|"
    r"buckle up|let'?s dive|dive in|skin[- ]loving|toxin[- ]free|chemical[- ]free|in today'?s)\b",
    re.IGNORECASE,
)

# Invented timing is a tell of an invented anecdote; allowed only if the note says it.
TIME_PHRASE_RE = re.compile(
    r"\b(this morning|this afternoon|tonight|yesterday|today|last (?:week|month|year|night|quarter)|"
    r"earlier this (?:week|month|year)|a few (?:days|weeks|months) ago|recently)\b",
    re.IGNORECASE,
)

NUMBER_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")(?:[- ](one|two|three|four|five|six|seven|eight|nine))?\b",
                             re.IGNORECASE)


_DIGIT_WORDS: dict[str, str] = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                                "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
# Spoken decimals such as "zero point four" or "point four": voice notes rarely contain digits.
_DECIMAL_WORD_RE = re.compile(r"(?:\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s+)?\bpoint((?:\s+(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\b)+)", re.IGNORECASE)


def _canonical(number: str) -> str:
    """'1,000' and '1000' match; '0.40' and '0.4' match."""
    number = number.replace(",", "")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number


def known_numbers(source_text: str) -> set[str]:
    """Every number the draft may use: digits in the sources plus spelled-out numbers ('batch fourteen')."""
    found = {_canonical(n) for n in NUMBER_RE.findall(source_text)}
    for whole, fraction in _DECIMAL_WORD_RE.findall(source_text):
        integer = str(NUMBER_WORDS[whole.lower()]) if whole else "0"
        digits = "".join(_DIGIT_WORDS[w.lower()] for w in fraction.split())
        found.add(_canonical(f"{integer}.{digits}"))
    # Spoken decimals are consumed first, so "zero point four" licenses 0.4 but not 0 or 4.
    remaining = _DECIMAL_WORD_RE.sub(" ", source_text)
    for tens, unit in _NUMBER_WORD_RE.findall(remaining):
        value = NUMBER_WORDS[tens.lower()] + (NUMBER_WORDS[unit.lower()] if unit else 0)
        found.add(str(value))
    return found


def unsupported_numbers(body: str, source_text: str) -> list[str]:
    allowed = known_numbers(source_text)
    return sorted({n for n in NUMBER_RE.findall(body) if _canonical(n) not in allowed})


def unsupported_research_claims(body: str, source_text: str) -> list[str]:
    source = source_text.lower()
    claims = []
    for match in STUDY_RE.finditer(body):
        keyword = next(g for g in match.groups() if g)
        if keyword.lower() not in source:
            claims.append(match.group(0))
    return claims


def validate(body: str, used_source_url: str, self_check: dict[str, Any],
             note_text: str, news: list[NewsItem]) -> list[str]:
    """Every reason this draft may not be sent. Empty list means it passes."""
    problems: list[str] = []
    settings = config.settings
    if not settings.draft_min_chars <= len(body) <= settings.draft_max_chars:
        problems.append(f"length is {len(body)} characters; it must be between "
                        f"{settings.draft_min_chars} and {settings.draft_max_chars}")

    cited = next((i for i in news if i.url == used_source_url), None) if used_source_url else None
    if used_source_url and cited is None:
        problems.append("used_source_url is not one of the provided news URLs; never invent a source")
    source_text = note_text + ("\n" + cited.title if cited else "")

    if numbers := unsupported_numbers(body, source_text):
        problems.append(f"contains numbers not in the note or cited headline: {', '.join(numbers)}")
    if claims := unsupported_research_claims(body, source_text):
        problems.append(f"cites research that is not in the note or cited headline: {'; '.join(claims)}")
    source_lower = source_text.lower()
    invented_times = sorted({m.lower() for m in TIME_PHRASE_RE.findall(body) if m.lower() not in source_lower})
    if invented_times:
        problems.append(f"adds timing not in the note: {', '.join(invented_times)}")
    if self_check.get("invented_stats") is True:
        problems.append("self-check reported invented statistics")
    if self_check.get("on_voice") is False:
        problems.append("self-check reported the post is off-voice")
    if EMOJI_RE.search(body):
        problems.append("contains emoji")
    if HASHTAG_RE.search(body):
        problems.append("contains hashtags")
    if URL_RE.search(body):
        problems.append("contains a link; links never go in the post body")
    if match := CTA_RE.search(body):
        problems.append(f"contains a call to action: '{match.group(0)}'")
    if match := CLICHE_RE.search(body):
        problems.append(f"contains off-voice cliché: '{match.group(0)}'")
    if len(LIST_LINE_RE.findall(body)) >= 2:
        problems.append("uses bullet or numbered lists; write in prose paragraphs")

    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    one_liners = sum(1 for p in paragraphs if len(re.findall(r"[.!?](?:\s|$)", p)) <= 1)
    if len(paragraphs) < 3:
        problems.append("needs at least three paragraphs")
    elif len(paragraphs) >= 4 and one_liners / len(paragraphs) > 0.5:
        problems.append("too many one-sentence paragraphs; write patient multi-sentence paragraphs")
    return problems


# --- drafting --------------------------------------------------------------------


def draft_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "body": {"type": "string"},
            "used_source_url": {"type": "string"},
            "news_relevance": {"type": "string"},
            "self_check": {
                "type": "object",
                "properties": {"invented_stats": {"type": "boolean"}, "on_voice": {"type": "boolean"}},
                "required": ["invented_stats", "on_voice"],
            },
        },
        "required": ["body", "used_source_url", "news_relevance", "self_check"],
    }


def qa_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"claim": {"type": "string"},
                                         "severity": {"type": "string", "enum": ["blocking", "minor"]},
                                         "why": {"type": "string"}},
                          "required": ["claim", "severity", "why"]},
            },
            "changes_meera_idea": {"type": "boolean"},
            "idea_note": {"type": "string"},
            "voice_issues": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["findings", "changes_meera_idea", "idea_note", "voice_issues"],
    }


def build_prompt(note_text: str, category: str | None, angle: str, exemplars: list[CorpusPiece],
                 news: list[NewsItem], feedback: str = "", core_idea: str = "") -> str:
    return gemini_client.render(
        gemini_client.load_prompt("draft"),
        voice_skill=gemini_client.load_prompt("voice_skill"),
        category=category or "any",
        angle=angle or "Develop the note's own point.",
        core_idea=core_idea or angle or "the point Meera makes in her note",
        exemplars="\n\n".join(format_exemplar(p) for p in exemplars),
        note=note_text,
        news=format_news(news),
        feedback=feedback,
    )


def _feedback(problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return f"\n# Your previous draft was rejected\nFix every one of these problems:\n{listed}\n"


def sources_text(note_text: str, cited: NewsItem | None) -> str:
    text = f"MEERA'S NOTE:\n{note_text}"
    if cited is not None:
        text += f"\n\nCITED HEADLINE ({cited.source}): {cited.title}"
    return text


async def fact_check(body: str, sources: str) -> tuple[list[str], dict[str, Any]]:
    """Second-pass QA by a model. Blocking findings or a changed idea become problems; minor ones are recorded.

    Anything the model doesn't clearly mark "minor" is treated as blocking (fail closed).
    """
    data = await gemini_client.generate_json(
        gemini_client.render(gemini_client.load_prompt("qa"), sources=sources, draft=body),
        qa_schema(), config.settings.qa_model,
    )
    findings = [f for f in data.get("findings") or [] if isinstance(f, dict) and f.get("claim")]
    blocking = [f for f in findings if f.get("severity") != "minor"]
    minor = [f for f in findings if f.get("severity") == "minor"]
    problems = [f"states something not in the note or source: '{f['claim']}' ({f.get('why', '')})" for f in blocking]
    if data.get("changes_meera_idea") is True:
        problems.append(f"changes Meera's idea: {data.get('idea_note', '')}")
    qa = {"passed": not problems, "blocking": blocking, "minor": minor,
          "changes_meera_idea": bool(data.get("changes_meera_idea")),
          "idea_note": str(data.get("idea_note", "")), "voice_issues": list(data.get("voice_issues") or [])}
    return problems, qa


async def _attempt(prompt: str, note_text: str, news: list[NewsItem]
                   ) -> tuple[str, NewsItem | None, str, list[str], dict[str, Any] | None]:
    """One draft: code validators first (cheap, certain), then the model fact check only if those pass."""
    data = await gemini_client.generate_json(prompt, draft_schema(), config.settings.draft_model)
    body = normalise(str(data.get("body", "")))
    used = str(data.get("used_source_url") or "").strip()
    relevance = str(data.get("news_relevance") or "").strip()
    self_check = data.get("self_check") if isinstance(data.get("self_check"), dict) else {}
    problems = validate(body, used, self_check, note_text, news)
    cited = next((i for i in news if i.url == used), None) if used else None
    if cited is not None and not relevance:
        problems.append("a cited news item needs news_relevance explaining how it connects to Meera's idea")
    if problems:
        return body, cited, relevance, problems, None
    qa_problems, qa = await fact_check(body, sources_text(note_text, cited))
    return body, cited, relevance, qa_problems, qa


async def make_draft(note_text: str, category: str | None, angle: str, exemplars: list[CorpusPiece],
                     news: list[NewsItem] | None = None, core_idea: str = "") -> DraftResult | None:
    """Draft, validate, fact-check; redraft once on any problem; None if it still fails (fail closed).

    Raises GeminiError if the model is unreachable, so callers can tell 'AI down' from 'draft rejected'.
    """
    news = news or []
    prompt = build_prompt(note_text, category, angle, exemplars, news, core_idea=core_idea)
    body, cited, relevance, problems, qa = await _attempt(prompt, note_text, news)
    if problems:
        log.warning("draft.rejected attempt=1 problems=%s", problems)
        prompt = build_prompt(note_text, category, angle, exemplars, news, _feedback(problems), core_idea)
        body, cited, relevance, problems, qa = await _attempt(prompt, note_text, news)
    if problems:
        log.warning("draft.rejected attempt=2 problems=%s action=drop", problems)
        return None
    return DraftResult(
        body=body, source_url=cited.url if cited else None, exemplar_ids=[p.id for p in exemplars],
        model=config.settings.draft_model, news=news_source.to_record(cited, relevance) if cited else None, qa=qa,
    )


async def revise_draft(note_text: str, previous_body: str, instruction: str) -> tuple[str, dict[str, Any]] | None:
    """Redraft once from Meera's one-line instruction under the same validators and fact check.

    Facts may come from the note or the previous draft (which already passed both checks).
    Returns (body, qa) or None if it fails. Raises GeminiError if the model is unreachable.
    """
    def prompt(feedback: str = "") -> str:
        return gemini_client.render(
            gemini_client.load_prompt("revise"),
            voice_skill=gemini_client.load_prompt("voice_skill"),
            instruction=instruction, previous=previous_body, note=note_text, feedback=feedback,
        )

    sources = note_text + "\n" + previous_body
    body, _, _, problems, qa = await _attempt(prompt(), sources, [])
    if problems:
        log.warning("draft.revision_rejected attempt=1 problems=%s", problems)
        body, _, _, problems, qa = await _attempt(prompt(_feedback(problems)), sources, [])
    if problems:
        log.warning("draft.revision_rejected attempt=2 problems=%s action=drop", problems)
        return None
    return body, qa


async def draft_note(note_text: str, category: str | None, angle: str, core_idea: str,
                     news_keywords: str) -> DraftResult | None:
    """The full drafting step for a qualified note: optional news hook, then draft + both QA layers.

    News never blocks drafting: fetch_news returns [] on any failure or when nothing credible fits.
    """
    items = await news_source.fetch_news(news_keywords)
    return await make_draft(note_text, category, angle, pick_exemplars(category), items, core_idea=core_idea)
