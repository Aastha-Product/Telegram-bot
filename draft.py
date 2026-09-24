"""Voice-matched drafting from a note plus corpus exemplars, with code-side validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CORPUS_DIR = Path(__file__).parent / "corpus"


@dataclass(frozen=True)
class CorpusPiece:
    id: int
    source_id: str
    format: str  # "linkedin" | "newsletter"
    category: str
    title: str
    body: str


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
