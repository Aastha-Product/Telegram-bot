"""Live evals against real Gemini/RSS (run by hand, not collected by pytest).

Usage:
    python tests/eval_live.py triage      # score the 5 sample notes
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


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "triage"
    asyncio.run({"triage": eval_triage}[command]())
