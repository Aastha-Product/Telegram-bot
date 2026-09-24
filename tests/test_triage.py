import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import config
import db
import draft
import gemini_client
import triage

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES: dict[str, str] = json.loads((FIXTURES / "sample_notes.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
THRESHOLD = config.settings.triage_threshold

# What a well-behaved model should roughly return for the sample notes (PLAN.md Phase 5).
EXPECTED_VERDICTS = {
    "batch_14": {"score": 8.5, "category": "Industry Transparency"},
    "cold_pressed": {"score": 8.0, "category": "Industry Transparency"},
    "layering_order": {"score": 7.5, "category": "Consumer Education"},
    "skin_barrier": {"score": 6.0, "category": "Formulation Science"},
    "clean_beauty": {"score": 3.5, "category": "Industry Transparency"},
}


def _verdict(score: float, category: str = "Formulation Science", worth: bool | None = None, **kw) -> dict:
    return {"worth_developing": score >= 6 if worth is None else worth, "score": score, "category": category,
            "reason": kw.get("reason", "r"), "suggested_angle": kw.get("angle", "a"),
            "news_keywords": kw.get("news_keywords", "cosmetic preservative India")}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path) -> None:
    db.init_db(tmp_path / "test.db")


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake Gemini: maps a substring of the note to a verdict; records every prompt."""
    state: dict = {"verdicts": {}, "prompts": [], "error": None}

    async def fake_generate_json(prompt: str, schema: dict, model_name: str) -> dict:
        assert model_name == config.settings.triage_model
        state["prompts"].append(prompt)
        if state["error"]:
            raise state["error"]
        for needle, verdict in state["verdicts"].items():
            if needle in prompt:
                return verdict
        raise AssertionError("unexpected note")

    monkeypatch.setattr(gemini_client, "generate_json", fake_generate_json)
    return state


def _add(text: str, minutes: int = 0, message_id: int | None = None) -> db.Note:
    note_id = db.add_note(message_id or (minutes + 1), -1001, text, T0 + timedelta(minutes=minutes))
    return db.get_note(note_id)


def _add_samples() -> dict[str, db.Note]:
    return {key: _add(text, minutes=i) for i, (key, text) in enumerate(SAMPLES.items())}


def _run(notes: list[db.Note]) -> list[triage.TriageResult]:
    return asyncio.run(triage.score_notes(notes))


# --- ranking on the sample notes --------------------------------------------------


def test_sample_notes_rank_as_expected(model: dict) -> None:
    notes = _add_samples()
    for key, v in EXPECTED_VERDICTS.items():
        model["verdicts"][SAMPLES[key][:60]] = _verdict(v["score"], v["category"])
    results = {r.note_id: r for r in _run(list(notes.values()))}

    best = triage.pick_best(list(results.values()), THRESHOLD)
    assert best.note_id == notes["batch_14"].id
    assert results[notes["clean_beauty"].id].score < results[notes["batch_14"].id].score
    assert not results[notes["clean_beauty"].id].is_eligible(THRESHOLD)
    for key in ("batch_14", "cold_pressed", "layering_order"):
        assert results[notes[key].id].is_eligible(THRESHOLD)


def test_verdicts_are_persisted_and_reused(model: dict) -> None:
    note = _add(SAMPLES["batch_14"])
    model["verdicts"]["batch fourteen"] = _verdict(8.5, "Industry Transparency", angle="same formula is not the same",
                                                   news_keywords="preservative supplier change")
    _run([note])
    stored = db.get_note(note.id)
    assert (stored.score, stored.worth_developing, stored.category) == (8.5, True, "Industry Transparency")
    assert stored.angle == "same formula is not the same"
    assert stored.news_keywords == "preservative supplier change"

    model["prompts"].clear()
    [again] = _run([stored])
    assert model["prompts"] == []  # never scored twice
    assert again.score == 8.5 and again.angle == "same formula is not the same"


# --- code-side gates ------------------------------------------------------------


def test_short_notes_rejected_without_model_call(model: dict) -> None:
    notes = [_add("look into this", 0), _add("https://example.com/article", 1)]
    results = _run(notes)
    assert model["prompts"] == []
    assert all(r.score == 0 and not r.worth_developing for r in results)
    assert db.get_note(notes[0].id).triage_reason == "too short to develop"


def test_low_score_is_never_worth_developing_even_if_model_says_so(model: dict) -> None:
    model["verdicts"]["clean beauty"] = _verdict(4.0, worth=True)
    [result] = _run([_add(SAMPLES["clean_beauty"])])
    assert result.worth_developing is False
    assert triage.pick_best([result], THRESHOLD) is None


def test_model_saying_not_worth_it_wins_over_high_score(model: dict) -> None:
    model["verdicts"]["cold-pressed"] = _verdict(8.0, worth=False)
    [result] = _run([_add(SAMPLES["cold_pressed"])])
    assert triage.pick_best([result], THRESHOLD) is None


@pytest.mark.parametrize("bad", [
    {"score": 11}, {"score": -1}, {"score": "high"}, {"worth_developing": "yes"},
])
def test_invalid_verdict_dropped_and_retried_later(model: dict, bad: dict) -> None:
    note = _add(SAMPLES["batch_14"])
    model["verdicts"]["batch fourteen"] = {**_verdict(8.0), **bad}
    assert _run([note]) == []
    assert db.get_note(note.id).score is None  # unscored, so the next run tries again


def test_unknown_category_becomes_none(model: dict) -> None:
    model["verdicts"]["batch fourteen"] = _verdict(8.0, category="Hot Takes")
    [result] = _run([_add(SAMPLES["batch_14"])])
    assert result.category is None and result.is_eligible(THRESHOLD)


def test_news_keywords_are_sanitised(model: dict) -> None:
    model["verdicts"]["batch fourteen"] = _verdict(
        8.0, news_keywords='"preservative" & supplier; https://evil.example/?q=1 India cosmetics extra words')
    [result] = _run([_add(SAMPLES["batch_14"])])
    assert result.news_keywords == "preservative supplier https evil example q"
    assert len(result.news_keywords) <= triage.MAX_KEYWORD_CHARS


def test_gemini_outage_propagates(model: dict) -> None:
    model["error"] = gemini_client.GeminiError("model call failed: ServerError 503 UNAVAILABLE")
    with pytest.raises(gemini_client.GeminiError):
        _run([_add(SAMPLES["batch_14"])])


# --- pick_best ------------------------------------------------------------------


def _result(note_id: int, score: float, worth: bool = True) -> triage.TriageResult:
    return triage.TriageResult(note_id, score, worth, "Founder Story", "", "", "")


def test_pick_best_threshold_and_ties() -> None:
    assert triage.pick_best([], THRESHOLD) is None
    assert triage.pick_best([_result(1, 5.9), _result(2, 2)], THRESHOLD) is None
    assert triage.pick_best([_result(1, 6.0)], THRESHOLD).note_id == 1  # threshold is inclusive
    assert triage.pick_best([_result(1, 7), _result(2, 9), _result(3, 9)], THRESHOLD).note_id == 2


# --- prompt & schema --------------------------------------------------------------


def test_prompt_contains_rubric_inputs() -> None:
    prompt = triage.build_prompt("my raw note")
    assert "<<<NOTE\nmy raw note\nNOTE>>>" in prompt
    for category in draft.corpus_categories():
        assert category in prompt
    assert "Clean beauty gets some things right" in prompt  # published titles let it spot repeats
    assert "{" + "note}" not in prompt


def test_schema_constrains_category_and_requires_all_fields() -> None:
    schema = triage.triage_schema()
    assert schema["properties"]["category"]["enum"] == draft.corpus_categories()
    assert set(schema["required"]) == set(schema["properties"])


def test_corpus_loads_all_published_pieces() -> None:
    pieces = draft.load_corpus()
    assert len(pieces) == 15
    assert sum(p.format == "linkedin" for p in pieces) == 4
    assert len(draft.corpus_categories()) == 7
    assert all(p.body and p.title for p in pieces)
