"""Live evals against real Gemini/RSS (run by hand, not collected by pytest).

Usage:
    python tests/eval_live.py triage      # score the 5 sample notes
    python tests/eval_live.py news        # real Google News fetch for sample keywords
    python tests/eval_live.py draft [--save]  # triage + news + draft for the 5 sample notes;
                                              # --save stores them as the prompt-regression baseline
    python tests/eval_live.py probe [n]   # hallucination probe: n drafts of one note, no news
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import draft  # noqa: E402
import news  # noqa: E402
import triage  # noqa: E402

SAMPLES: dict[str, str] = json.loads(
    (Path(__file__).parent / "fixtures" / "sample_notes.json").read_text(encoding="utf-8"))


def _load_samples() -> dict[str, db.Note]:
    db.init_db(Path(tempfile.mkdtemp()) / "eval.db")
    t0 = datetime.now(UTC)
    return {key: db.get_note(db.add_note(i + 1, -100, text, t0 + timedelta(minutes=i)))
            for i, (key, text) in enumerate(SAMPLES.items())}


async def eval_triage() -> None:
    notes = _load_samples()
    results = {r.note_id: r for r in await triage.score_notes(list(notes.values()))}
    for key, note in sorted(notes.items(), key=lambda kv: -results[kv[1].id].score):
        r = results[note.id]
        print(f"{r.score:4.1f}  eligible={r.is_eligible(config.settings.triage_threshold)!s:5}  "
              f"{key:15} {r.category}  | news: {r.news_keywords}")
        print(f"      reason: {r.reason}\n      angle:  {r.angle}")
    best = triage.pick_best(list(results.values()), config.settings.triage_threshold)
    print("pick_best ->", next(k for k, n in notes.items() if best and n.id == best.note_id))
    batch, clean = results[notes["batch_14"].id], results[notes["clean_beauty"].id]
    print("CHECK clean_beauty ranks below batch_14:", clean.score < batch.score)


NEWS_QUERIES = [
    "cosmetic raw material supplier formulation changes",
    "cosmetic ingredient sourcing India supplier audit",
    "skin barrier repair ceramide",
    "skincare cosmetics India regulation",
    "sunscreen SPF India",
]


async def eval_news() -> None:
    for query in NEWS_QUERIES:
        raw = news.parse_items(await news._fetch(news.build_params(query)))
        kept = await news.fetch_news(query)
        print(f"[{query}] raw={len(raw)} kept={len(kept)}")
        for item in kept:
            print(f"   {item.published:%Y-%m-%d} {item.source}: {item.title[:90]}")


BASELINE = Path(__file__).parent / "fixtures" / "last_good_drafts.json"


async def eval_draft(save: bool = False) -> None:
    notes = _load_samples()
    baseline: dict[str, dict] = {}
    for key, note in notes.items():
        verdict = await triage.score_note(note)
        items = await news.fetch_news(verdict.news_keywords)
        result = await draft.make_draft(note.content, verdict.category, verdict.angle,
                                        draft.pick_exemplars(verdict.category), items)
        print("=" * 100)
        print(f"{key}: score={verdict.score} category={verdict.category} news_offered={len(items)}")
        if result is None:
            print("  -> DROPPED (failed validation twice)")
            continue
        words = len(result.body.split())
        print(f"  words={words} chars={len(result.body)} source={result.source_url}")
        print("-" * 100)
        print(result.body)
        baseline[key] = {"score": verdict.score, "category": verdict.category, "body": result.body}
    if save:
        BASELINE.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"saved {len(baseline)} drafts to {BASELINE}")


async def eval_probe(runs: int) -> None:
    note = SAMPLES["layering_order"]
    passed = dropped = 0
    for n in range(runs):
        result = await draft.make_draft(note, "Consumer Education", "Layering order, not the serum, is the problem.",
                                        draft.pick_exemplars("Consumer Education"), [])
        if result is None:
            dropped += 1
        else:
            passed += 1
            assert not draft.unsupported_numbers(result.body, note)
        print(f"run {n + 1}: {'dropped' if result is None else 'passed validation'}")
    print(f"probe: {passed} passed, {dropped} dropped, 0 invented facts reached output")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "triage"
    if command == "probe":
        asyncio.run(eval_probe(int(sys.argv[2]) if len(sys.argv) > 2 else 5))
    elif command == "draft":
        asyncio.run(eval_draft(save="--save" in sys.argv))
    else:
        asyncio.run({"triage": eval_triage, "news": eval_news}[command]())
