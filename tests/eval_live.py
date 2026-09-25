"""Live evals against real Gemini / Google News (run by hand, not collected by pytest).

Usage:
    python tests/eval_live.py triage [keys...]     # 10-parameter scorecards for samples + test cases
    python tests/eval_live.py draft [--save]       # triage, then news + draft + QA for qualifying notes
    python tests/eval_live.py qa                   # TEST 9: inject a hallucination, confirm live QA catches it
    python tests/eval_live.py probe [n]            # hallucination probe: n drafts of one note, no news
    python tests/eval_live.py news                 # real Google News fetch for sample keywords
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import draft  # noqa: E402
import news  # noqa: E402
import triage  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES: dict[str, str] = json.loads((FIXTURES / "sample_notes.json").read_text(encoding="utf-8"))
CASES: dict[str, str] = json.loads((FIXTURES / "test_cases.json").read_text(encoding="utf-8"))
BASELINE = FIXTURES / "last_good_drafts.json"


def _load(notes: dict[str, str]) -> dict[str, db.Note]:
    db.init_db(Path(tempfile.mkdtemp()) / "eval.db")
    t0 = datetime.now(UTC)
    return {key: db.get_note(db.add_note(i + 1, -100, text, t0 + timedelta(minutes=i)))
            for i, (key, text) in enumerate(notes.items())}


def _print_scorecard(key: str, r: triage.TriageResult) -> None:
    print("=" * 96)
    print(f"{key}: overall={r.overall} decision={r.decision.upper()} category={r.category}")
    for p in r.parameters:
        caps = f" CAPPED({'; '.join(p.caps)})" if p.caps else ""
        print(f"   {p.name:21} raw={p.raw_score:4.1f} final={p.score:4.1f} x{p.weight} [{p.guardrail}]"
              f" evidence={'verified' if p.evidence_verified else 'NOT FOUND'}{caps}")
    for f in r.hard_flags:
        print(f"   FLAG {f['type']} ({f['source']}): {f['detail'][:90]}")


async def eval_triage(keys: list[str]) -> None:
    notes = _load({k: v for k, v in {**SAMPLES, **CASES}.items() if not keys or k in keys})
    for key, note in notes.items():
        r = await triage.assess_note(note)
        if r is None:
            print(f"{key}: INVALID ASSESSMENT")
        else:
            _print_scorecard(key, r)


async def eval_draft(save: bool) -> None:
    notes = _load(SAMPLES)
    baseline: dict[str, dict] = {}
    for key, note in notes.items():
        r = await triage.assess_note(note)
        print("=" * 96)
        print(f"{key}: overall={r.overall} decision={r.decision.upper()}")
        if not r.qualified:
            continue
        items = await news.fetch_news(r.news_keywords)
        result = await draft.make_draft(note.content, r.category, r.angle, draft.pick_exemplars(r.category), items,
                                        core_idea=r.summary.get("core_idea", ""))
        print(f"  news offered={len(items)} used={'yes' if result and result.news else 'no'}")
        if result is None:
            print("  -> DROPPED (failed code validation or fact check twice)")
            continue
        print(f"  words={len(result.body.split())} qa_passed={result.qa['passed']} "
              f"voice_issues={result.qa['voice_issues']}")
        if result.news:
            print(f"  NEWS: {result.news['headline']} | {result.news['source']} | {result.news['date']}")
            print(f"        relevance: {result.news['relevance']}")
        print("-" * 96)
        print(result.body)
        baseline[key] = {"overall": r.overall, "category": r.category, "body": result.body, "news": result.news}
    if save:
        BASELINE.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"saved {len(baseline)} drafts to {BASELINE}")


async def eval_qa() -> None:
    """TEST 9 live: a draft that passes every code check but contains an invented story."""
    note = SAMPLES["batch_14"]
    clean = json.loads(BASELINE.read_text(encoding="utf-8"))["batch_14"]["body"] if BASELINE.exists() else None
    base = clean or ("Batch fourteen came back from the manufacturer and the pH stability data looked off. "
                     "The supplier had quietly changed the preservative blend. The finished product pH dropped by "
                     "about 0.4 units, enough to push us out of the optimal range for our emollient blend.")
    injected = base + ("\n\nWhen I told our head of quality, she said three other brands she knows had exactly "
                       "the same thing happen with that supplier, and one of them had to recall a product.")
    code = draft.validate(injected, "", {"invented_stats": False, "on_voice": True}, note, [])
    problems, qa = await draft.fact_check(injected, draft.sources_text(note, None))
    print("code validators:", code or "passed (cannot see the invented story)")
    print("live QA blocking findings:")
    for c in qa["blocking"]:
        print("  -", c["claim"][:110], "|", c["why"][:90])
    print("CAUGHT BY QA:", bool(problems))
    clean_problems, _ = await draft.fact_check(base, draft.sources_text(note, None))
    print("clean baseline passes QA:", not clean_problems, clean_problems[:2])


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
        print(f"run {n + 1}: {'dropped' if result is None else 'passed code checks + QA'}")
    print(f"probe: {passed} passed, {dropped} dropped")


async def eval_news() -> None:
    for query in ["cosmetic preservative supplier", "skin barrier repair ceramide", "sunscreen SPF India",
                  "skincare cosmetics India regulation"]:
        kept = await news.fetch_news(query)
        print(f"[{query}] credible+relevant={len(kept)}")
        for item in kept:
            print(f"   {item.published:%Y-%m-%d} {item.source}: {item.title[:90]}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "triage"
    args = sys.argv[2:]
    if command == "triage":
        asyncio.run(eval_triage(args))
    elif command == "draft":
        asyncio.run(eval_draft("--save" in args))
    elif command == "qa":
        asyncio.run(eval_qa())
    elif command == "probe":
        asyncio.run(eval_probe(int(args[0]) if args else 5))
    else:
        asyncio.run(eval_news())
