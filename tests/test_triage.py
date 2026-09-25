import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import config
import db
import draft
import gemini_client
import triage

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES: dict[str, str] = json.loads((FIXTURES / "sample_notes.json").read_text(encoding="utf-8"))
CASES: dict[str, str] = json.loads((FIXTURES / "test_cases.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
NOTE = SAMPLES["batch_14"]
QUOTE = "the finished product pH dropped by about 0.4 units"


def reply(scores: float | dict[str, float] = 9.0, guardrails: dict[str, str] | None = None,
          evidence: str = QUOTE, flags: list[dict] | None = None, drop: str | None = None) -> dict:
    """A well-formed model answer; override per-parameter scores/guardrails as needed."""
    def score(key: str) -> float:
        return scores.get(key, 9.0) if isinstance(scores, dict) else scores

    params = [{"key": k, "score": score(k), "reason": f"reason {k}", "evidence": evidence, "gap": f"gap {k}",
               "guardrail": (guardrails or {}).get(k, "pass"), "guardrail_note": ""}
              for k in triage.PARAMETER_KEYS if k != drop]
    return {
        "summary": {"core_idea": "same formula is not the same formula", "founder_perspective": "formulator",
                    "intended_audience": "skincare buyers", "main_insight": "check the CoA every batch",
                    "topic": "supplier changes"},
        "parameters": params,
        "hard_flags": flags or [],
        "category": "Industry Transparency",
        "suggested_angle": "a reorder is not always the same formula",
        "news_keywords": "cosmetic preservative supplier",
    }


def _param(score: float, weight: float = 1.0, key: str = "x") -> triage.ParameterScore:
    return triage.ParameterScore(key, key, weight, score, score, "", "", True, "", "pass", "")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Path) -> None:
    db.init_db(tmp_path / "test.db")


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"replies": [], "prompts": []}

    async def fake_generate_json(prompt: str, schema: dict, model_name: str, attachments=None) -> dict:
        assert model_name == config.settings.triage_model
        state["prompts"].append(prompt)
        item = state["replies"].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(gemini_client, "generate_json", fake_generate_json)
    return state


def _note(text: str = NOTE, **kw) -> db.Note:
    return db.get_note(db.add_note(kw.pop("message_id", 1), -1001, text, T0, **kw))


def _assess(note: db.Note) -> triage.TriageResult | None:
    return asyncio.run(triage.assess_note(note))


# --- rubric definition -------------------------------------------------------------------


def test_exactly_ten_parameters_with_documented_weights() -> None:
    assert len(triage.PARAMETERS) == 10 == len(set(triage.PARAMETER_KEYS))
    weights = {p.name: p.weight for p in triage.PARAMETERS}
    assert weights == {"Founder Relevance": 1.0, "Audience Relevance": 1.0, "Originality": 1.5,
                       "Insight Depth": 1.0, "Authenticity": 1.5, "Practical Value": 1.0, "Credibility": 1.5,
                       "LinkedIn Fit": 0.5, "Discussion Potential": 0.5, "Postability": 1.5}
    assert all(p.guardrail for p in triage.PARAMETERS)  # every parameter has its own guardrail


# --- scoring & decision (deterministic, in code) --------------------------------------------


def test_overall_is_reproducible_weighted_average() -> None:
    params = (_param(10, 1.5), _param(6, 0.5))
    assert triage.weighted_overall(params) == 9.0  # (15 + 3) / 2


def test_overall_rounds_half_up_to_one_decimal() -> None:
    assert triage.weighted_overall((_param(8.05),)) == 8.1
    assert triage.weighted_overall((_param(8.04),)) == 8.0


def test_score_exactly_8_does_not_qualify() -> None:  # TEST 7
    assert config.settings.triage_threshold == 8.0
    assert triage.decide(8.0, []) == triage.REJECTED


def test_score_8_1_qualifies_when_guardrails_pass() -> None:  # TEST 8
    assert triage.decide(8.1, []) == triage.QUALIFIED


def test_guardrail_failure_overrides_a_high_score() -> None:
    assert triage.decide(9.2, [{"type": "confidential_information"}]) == triage.HUMAN_REVIEW


# --- parsing & code-side guardrails ---------------------------------------------------------


def test_valid_reply_parses_into_a_full_scorecard() -> None:
    result = triage.parse_assessment(_note(), reply(9.0), "m")
    assert [p.key for p in result.parameters] == list(triage.PARAMETER_KEYS)
    assert result.overall == 9.0 and result.decision == triage.QUALIFIED
    assert all(p.evidence_verified for p in result.parameters)
    assert result.summary["core_idea"] and result.category == "Industry Transparency"


@pytest.mark.parametrize("broken", [
    lambda r: r.update(parameters=r["parameters"][:9]),
    lambda r: r.update(parameters=r["parameters"] + [r["parameters"][0]]),
    lambda r: r["parameters"][0].update(score=11),
    lambda r: r["parameters"][0].update(score="high"),
    lambda r: r["parameters"][0].update(guardrail="maybe"),
    lambda r: r["parameters"][0].update(key="vibes"),
    lambda r: r.update(parameters="all good"),
])
def test_malformed_scorecards_are_rejected(broken) -> None:
    data = reply()
    broken(data)
    assert triage.parse_assessment(_note(), data, "m") is None


def test_unverifiable_evidence_caps_parameter_at_5() -> None:
    result = triage.parse_assessment(_note(), reply(9.0, evidence="she ran a clinical trial with 500 women"), "m")
    assert all(p.score == 5.0 and not p.evidence_verified for p in result.parameters)
    assert result.parameters[0].caps == ("evidence not found in transcript",)
    assert result.decision == triage.REJECTED


def test_evidence_check_rules() -> None:
    assert triage.evidence_is_verified(QUOTE, NOTE)
    assert triage.evidence_is_verified("The finished product pH dropped, by about 0.4 units!", NOTE)
    assert not triage.evidence_is_verified("pH dropped", NOTE)  # too short to count
    assert not triage.evidence_is_verified("", NOTE)


def test_guardrail_caps_scores() -> None:
    result = triage.parse_assessment(
        _note(), reply(9.0, guardrails={"credibility": "warn", "postability": "fail"}), "m")
    by_key = {p.key: p for p in result.parameters}
    assert by_key["credibility"].score == 6.0 and by_key["credibility"].raw_score == 9.0
    assert by_key["postability"].score == 2.0
    assert by_key["originality"].score == 9.0
    assert result.overall == triage.weighted_overall(result.parameters)


def test_model_hard_flags_force_human_review_even_at_high_score() -> None:
    flag = {"type": "defamatory", "detail": "accuses a named supplier", "quote": "they quietly changed"}
    result = triage.parse_assessment(_note(), reply(9.5, flags=[flag, {"type": "made_up", "detail": "", "quote": ""}]), "m")
    assert result.overall == 9.5 and result.decision == triage.HUMAN_REVIEW
    assert [f["type"] for f in result.hard_flags] == ["defamatory"]  # unknown flag types are dropped
    assert result.hard_flags[0]["source"] == "model"


@pytest.mark.parametrize(("text", "label"), [
    ("reach me at priya.sharma84@gmail.com please", "email"),
    ("her number is +91 98765 43210", "phone"),
    ("call 9876543210 tomorrow", "phone"),
    ("ID 1234 5678 9012 was on the form", "ID-like"),
])
def test_personal_data_is_flagged_by_code(text: str, label: str) -> None:
    flags = triage.personal_data_flags(text)
    assert flags and label in flags[0]["detail"] and flags[0]["source"] == "code"


@pytest.mark.parametrize("text", [NOTE, SAMPLES["cold_pressed"], "In 2026 we saw 1,450 rupees and 23% returns"])
def test_ordinary_numbers_are_not_personal_data(text: str) -> None:
    assert triage.personal_data_flags(text) == []


def test_confidential_case_is_human_review_via_code_even_if_model_misses_it() -> None:  # TEST 6
    result = triage.parse_assessment(_note(CASES["t6_confidential"]), reply(9.0, evidence="our supplier Aroma Chem in Vapi quoted us"), "m")
    assert result.decision == triage.HUMAN_REVIEW
    assert any(f["type"] == "personal_data_detected" for f in result.hard_flags)


def test_improvement_tips_come_from_weakest_parameters() -> None:
    result = triage.parse_assessment(_note(), reply({"originality": 2, "credibility": 3, "postability": 4}), "m")
    assert result.improvements == ("Originality: gap originality", "Credibility: gap credibility",
                                   "Postability: gap postability")


def test_news_keywords_are_sanitised() -> None:
    data = reply()
    data["news_keywords"] = '"preservative" & supplier; https://evil.example/?q=1 India more words'
    result = triage.parse_assessment(_note(), data, "m")
    assert result.news_keywords == "preservative supplier https evil example q"


# --- assess_note (model mocked) --------------------------------------------------------------


def test_assessment_is_persisted_and_reused(model: dict) -> None:
    note = _note()
    model["replies"].append(reply(9.0))
    first = _assess(note)
    stored = db.get_latest_assessment(note.id)
    assert stored.decision == "qualified" and stored.overall == 9.0 and len(stored.parameters) == 10
    assert stored.rubric_version == triage.RUBRIC_VERSION and stored.topic == "supplier changes"
    assert db.get_note(note.id).score == 9.0 and db.get_note(note.id).worth_developing is True
    again = _assess(db.get_note(note.id))
    assert len(model["prompts"]) == 1  # never scored twice
    assert again.overall == first.overall and again.decision == first.decision


def test_short_note_rejected_without_model_call(model: dict) -> None:
    result = _assess(_note("look into this"))
    assert model["prompts"] == [] and result.decision == triage.REJECTED and result.overall == 0.0


def test_low_confidence_voice_goes_to_human_review_without_scoring(model: dict) -> None:
    note_id = db.add_note(1, -1001, "", T0, "voice", "file", "pending_transcription")
    db.set_note_transcript(note_id, "the batch [unclear] came [unclear] back " * 3, "low", 0.4)
    result = _assess(db.get_note(note_id))
    assert model["prompts"] == []
    assert result.decision == triage.HUMAN_REVIEW
    assert result.hard_flags[0]["type"] == "low_confidence_transcript"


def test_unusable_model_answer_is_not_persisted(model: dict) -> None:
    note = _note()
    model["replies"].append(reply(drop="postability"))
    assert _assess(note) is None
    assert db.get_latest_assessment(note.id) is None  # retried on the next sweep


def test_gemini_outage_propagates(model: dict) -> None:
    model["replies"].append(gemini_client.GeminiError("model call failed: ServerError 503 UNAVAILABLE"))
    with pytest.raises(gemini_client.GeminiError):
        _assess(_note())


# --- prompt & schema -------------------------------------------------------------------------


def test_prompt_contains_rubric_flags_and_note() -> None:
    prompt = triage.build_prompt("my raw note")
    assert "<<<NOTE\nmy raw note\nNOTE>>>" in prompt
    for p in triage.PARAMETERS:
        assert f"{p.key} ({p.name})" in prompt
    assert "confidential_information" in prompt and "Clean beauty gets some things right" in prompt
    assert "{" not in prompt.replace("{" + "}", "")  # every placeholder filled


def test_schema_requires_ten_keyed_parameters_and_flags() -> None:
    schema = triage.triage_schema()
    item = schema["properties"]["parameters"]["items"]
    assert item["properties"]["key"]["enum"] == list(triage.PARAMETER_KEYS)
    assert set(item["required"]) == {"key", "score", "reason", "evidence", "gap", "guardrail", "guardrail_note"}
    assert schema["properties"]["category"]["enum"] == draft.corpus_categories()


def test_corpus_loads_all_published_pieces() -> None:
    pieces = draft.load_corpus()
    assert len(pieces) == 15 and sum(p.format == "linkedin" for p in pieces) == 4
