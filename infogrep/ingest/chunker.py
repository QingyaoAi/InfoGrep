"""Passage chunker: splits extracted pages into overlapping passages.

Chunking is word-based and done per page, so page/slide numbers stay accurate and
character offsets point back into the source page for citation. A sliding window of
``size`` words advances by ``size - overlap`` words each step.
"""

from __future__ import annotations

import re

from ..config import ChunkConfig
from .types import ExtractedPage, Passage

# Split on whitespace but keep offsets: iterate word matches with their positions.
_WORD_RE = re.compile(r"\S+")


def chunk_pages(doc_id: str, pages: list[ExtractedPage], config: ChunkConfig) -> list[Passage]:
    """Turn a document's extracted pages into a flat list of passages."""
    size = max(1, config.size)
    step = max(1, size - config.overlap)

    passages: list[Passage] = []
    ordinal = 0
    for page in pages:
        words = [(m.group(0), m.start()) for m in _WORD_RE.finditer(page.text)]
        if not words:
            continue
        for start in range(0, len(words), step):
            window = words[start : start + size]
            if not window:
                break
            offset = window[0][1]
            text = " ".join(w for w, _ in window)
            passages.append(
                Passage(
                    passage_id=f"{doc_id}#{ordinal}",
                    doc_id=doc_id,
                    path=doc_id,
                    ordinal=ordinal,
                    text=text,
                    page=page.page,
                    offset=offset,
                )
            )
            ordinal += 1
            # Last window reached the end; avoid emitting a redundant tail chunk.
            if start + size >= len(words):
                break
    return passages
