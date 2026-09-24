import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import config
import draft
import gemini_client
from news import NewsItem

SAMPLES: dict[str, str] = json.loads((Path(__file__).parent / "fixtures" / "sample_notes.json").read_text(encoding="utf-8"))
NOTE = SAMPLES["batch_14"]
NEWS = [NewsItem("CDSCO flags 12 cosmetic batches over preservative changes", "The Hindu",
                 "https://news.google.com/rss/articles/abc", datetime(2026, 9, 22, tzinfo=UTC))]

GOOD_BODY = """Batch 14 came back from our manufacturer, and the pH stability data looked off. Not dramatically, but enough that I went back to the supplier and asked what had changed.

It turned out they had quietly changed the preservative blend. They had sent a revised spec sheet three months ago, and it got buried. The new preservative system is more acidic than the old one, and the finished product pH dropped by about 0.4 units.

That sounds small. It is enough to push the formula out of the optimal range for our emollient blend. The batch isn't unsafe, but the texture is different in a way I think customers would notice, so we're holding it.

I'm not saying suppliers are acting in bad faith. I'm saying a 'same formula' reorder is often not the same formula, and the only way to know is to check the CoA against a baseline on every batch. If you don't, you find out when the customer does."""


def _check(body: str = GOOD_BODY, used: str = "", self_check: dict | None = None,
           note: str = NOTE, news: list[NewsItem] | None = None) -> list[str]:
    return draft.validate(body, used, self_check or {"invented_stats": False, "on_voice": True}, note, news or [])


# --- validators -------------------------------------------------------------------


def test_good_draft_passes() -> None:
    assert _check() == []


def test_fabricated_statistic_is_caught() -> None:
    body = GOOD_BODY.replace(
        "That sounds small.",
        "A 2026 study of 5,000 women found that 73% of them noticed texture changes like this. That sounds small.")
    problems = _check(body)
    assert any("numbers not in the note" in p and "2026" in p and "5,000" in p and "73" in p for p in problems)
    assert any("cites research" in p for p in problems)


def test_research_claim_without_numbers_is_caught() -> None:
    body = GOOD_BODY.replace("That sounds small.", "Research shows most brands never check. That sounds small.")
    assert any("cites research" in p for p in _check(body))


def test_research_claim_allowed_when_the_note_mentions_it() -> None:
    body = GOOD_BODY.replace("That sounds small.", "Research shows most brands never check. That sounds small.")
    assert _check(body, note=NOTE + " There is research on this.") == []


def test_spelled_out_numbers_in_note_may_appear_as_digits() -> None:
    assert draft.unsupported_numbers("batch 14, 0.40 units, 3 months", NOTE) == []
    assert draft.unsupported_numbers("batch 15", NOTE) == ["15"]
    assert draft.unsupported_numbers("twenty-four hours: 24", "it took twenty-four hours") == []


def test_numbers_from_cited_headline_allowed_only_when_cited() -> None:
    body = GOOD_BODY.replace("That sounds small.", "The Hindu reported that 12 batches were flagged. That sounds small.")
    assert any("numbers not in the note" in p for p in _check(body, news=NEWS))
    assert _check(body, used=NEWS[0].url, news=NEWS) == []


def test_invented_source_url_is_rejected() -> None:
    problems = _check(used="https://made-up.example/story", news=NEWS)
    assert any("not one of the provided news URLs" in p for p in problems)


@pytest.mark.parametrize(("edit", "expected"), [
    (lambda b: b + " \U0001F9EA", "emoji"),
    (lambda b: b + "\n\n#skincare #science", "hashtags"),
    (lambda b: b + " Read more at https://skinstinct.in", "link"),
    (lambda b: b + " DM me if you want the checklist.", "call to action"),
    (lambda b: b + " Thoughts?", "call to action"),
    (lambda b: "Here's the thing. " + b, "cliché"),
    (lambda b: b.replace("That sounds small.", "This is a game-changer. That sounds small."), "cliché"),
    (lambda b: b + "\n\n- check the CoA\n- compare to baseline\n- hold the batch", "lists"),
])
def test_voice_rules_enforced(edit, expected: str) -> None:
    assert any(expected in p for p in _check(edit(GOOD_BODY)))


def test_length_bounds() -> None:
    assert any("length" in p for p in _check("Too short.\n\nStill short.\n\nShort."))
    assert any("length" in p for p in _check(GOOD_BODY + ("\n\n" + GOOD_BODY) * 3))


def test_paragraph_shape() -> None:
    assert any("three paragraphs" in p for p in _check(GOOD_BODY.replace("\n\n", " ")))
    broetry = "\n\n".join(["Batch 14 came back with a pH drift."] * 8)
    assert any("one-sentence paragraphs" in p for p in _check(broetry))


def test_self_check_flags_reject() -> None:
    assert any("invented statistics" in p for p in _check(self_check={"invented_stats": True, "on_voice": True}))
    assert any("off-voice" in p for p in _check(self_check={"invented_stats": False, "on_voice": False}))


# --- normalisation ------------------------------------------------------------------


def test_normalise_dashes_spelling_and_markdown() -> None:
    text = "The **moisturizer** changed color—and its Behavior too – oddly.\n\n\n\nNext."
    assert draft.normalise(text) == "The moisturiser changed colour - and its Behaviour too - oddly.\n\nNext."


def test_normalise_leaves_british_words_and_lookalikes_alone() -> None:
    text = "The size of the colourful behaviour centre."
    assert draft.normalise(text) == text


# --- exemplars & prompt ---------------------------------------------------------------


def test_exemplars_prefer_same_category_and_include_linkedin() -> None:
    picks = draft.pick_exemplars("Formulation Science")
    assert len(picks) == 3
    assert sum(p.category == "Formulation Science" for p in picks) == 2
    assert any(p.format == "linkedin" for p in picks)


def test_exemplars_for_category_with_a_linkedin_post() -> None:
    picks = draft.pick_exemplars("Industry Transparency")
    assert all(p.category == "Industry Transparency" for p in picks)
    assert picks[0].format == "linkedin"


def test_exemplars_for_unknown_category_fall_back_to_linkedin() -> None:
    picks = draft.pick_exemplars(None)
    assert [p.format for p in picks] == ["linkedin"] * 3


def test_exemplar_formatting_drops_newsletter_greeting_and_signoff() -> None:
    newsletter = next(p for p in draft.load_corpus() if p.format == "newsletter")
    text = draft.format_exemplar(newsletter)
    assert not text.splitlines()[1].startswith("Hi,")
    assert not text.rstrip().endswith("Meera")


def test_prompt_has_skill_examples_note_and_news() -> None:
    prompt = draft.build_prompt(NOTE, "Industry Transparency", "same formula is not the same formula",
                                draft.pick_exemplars("Industry Transparency"), NEWS)
    assert "Skill: writing as Meera Pillai" in prompt
    assert "<<<NOTE\n" + NOTE + "\nNOTE>>>" in prompt
    assert NEWS[0].url in prompt and "The Hindu" in prompt
    assert "same formula is not the same formula" in prompt
    assert "--- Example (linkedin, Industry Transparency) ---" in prompt
    assert "{" + "note}" not in prompt and "{" + "feedback}" not in prompt


def test_prompt_without_news_forbids_mentioning_news() -> None:
    prompt = draft.build_prompt(NOTE, None, "", draft.pick_exemplars(None), [])
    assert "None available. Do not mention any news." in prompt


# --- make_draft (Gemini mocked) ---------------------------------------------------------


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"replies": [], "prompts": []}

    async def fake_generate_json(prompt: str, schema: dict, model_name: str) -> dict:
        assert model_name == config.settings.draft_model
        assert schema["required"] == ["body", "used_source_url", "self_check"]
        state["prompts"].append(prompt)
        reply = state["replies"].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(gemini_client, "generate_json", fake_generate_json)
    return state


def _reply(body: str = GOOD_BODY, used: str = "", invented: bool = False) -> dict:
    return {"body": body, "used_source_url": used, "self_check": {"invented_stats": invented, "on_voice": True}}


def _make(news: list[NewsItem] | None = None) -> draft.DraftResult | None:
    exemplars = draft.pick_exemplars("Industry Transparency")
    return asyncio.run(draft.make_draft(NOTE, "Industry Transparency", "angle", exemplars, news))


def test_make_draft_success(model: dict) -> None:
    model["replies"].append(_reply(GOOD_BODY.replace("That sounds small.", "That sounds small—it isn't.")))
    result = _make()
    assert result.body.count(" - ") == 1 and "—" not in result.body  # normalised
    assert result.source_url is None
    assert result.exemplar_ids == [p.id for p in draft.pick_exemplars("Industry Transparency")]
    assert result.model == config.settings.draft_model
    assert len(model["prompts"]) == 1


def test_make_draft_redrafts_once_with_feedback(model: dict) -> None:
    fabricated = GOOD_BODY.replace("That sounds small.", "A 2026 study found 73% agree. That sounds small.")
    model["replies"] += [_reply(fabricated), _reply()]
    assert _make().body == GOOD_BODY
    assert "Your previous draft was rejected" not in model["prompts"][0]
    assert "Your previous draft was rejected" in model["prompts"][1]
    assert "2026" in model["prompts"][1] and "73" in model["prompts"][1]


def test_make_draft_fails_closed_after_second_rejection(model: dict, caplog) -> None:
    model["replies"] += [_reply(invented=True), _reply(GOOD_BODY + " #skincare")]
    assert _make() is None
    assert len(model["prompts"]) == 2  # exactly one redraft, never a loop
    assert "action=drop" in caplog.text


def test_make_draft_keeps_a_real_cited_source(model: dict) -> None:
    body = GOOD_BODY.replace("That sounds small.", "The Hindu reported that 12 batches were flagged. That sounds small.")
    model["replies"].append(_reply(body, used=NEWS[0].url))
    assert _make(NEWS).source_url == NEWS[0].url


def test_make_draft_rejects_hallucinated_source_then_drops(model: dict) -> None:
    model["replies"] += [_reply(used="https://fake.example/a"), _reply(used="https://fake.example/b")]
    assert _make(NEWS) is None


def test_make_draft_propagates_gemini_outage(model: dict) -> None:
    model["replies"].append(gemini_client.GeminiError("model call failed: ServerError 503 UNAVAILABLE"))
    with pytest.raises(gemini_client.GeminiError):
        _make()


def test_invented_timing_is_caught_but_note_timing_allowed() -> None:
    assert any("timing not in the note: last week" in p for p in _check("Last week, " + GOOD_BODY))
    assert any("this morning" in p for p in _check(GOOD_BODY + " I checked this again this morning."))
    layering = SAMPLES["layering_order"]  # the note itself says "today"
    assert draft.TIME_PHRASE_RE.search("I heard from a customer today")
    assert not any("timing" in p for p in _check(GOOD_BODY + " She wrote to me today.", note=NOTE + " " + layering))


def test_voice_skill_forbids_invented_scenes_and_copying() -> None:
    skill = gemini_client.load_prompt("voice_skill")
    assert "The scene must come from the note" in skill
    assert "Copy sentences or signature lines" in skill
