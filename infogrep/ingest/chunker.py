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

# Control characters except tab/newline/carriage-return (NUL et al.).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Below this fraction of letters/digits/whitespace, treat a page as a failed/binary
# extraction (e.g. textutil dumping embedded objects from a .doc) and drop it.
_MIN_TEXTUAL_RATIO = 0.5


def clean_text(text: str) -> str:
    """Strip control characters (NUL, etc.) that bloat the index and pollute search."""
    return _CONTROL_RE.sub("", text)


def _is_cjk(ch: str) -> bool:
    return (
        "㐀" <= ch <= "鿿"  # CJK ideographs
        or "豈" <= ch <= "﫿"  # CJK compatibility
        or "぀" <= ch <= "ヿ"  # kana
        or "가" <= ch <= "힣"  # hangul
    )


def _is_mostly_textual(text: str) -> bool:
    """Reject pages that look like a failed/binary extraction (embedded object dumps).

    Real prose is mostly letters/digits/spaces; Latin-script text also has whitespace
    structure (spaces between words). Binary-as-text is random letters+symbols with no
    spaces — caught here. CJK has no spaces but is genuinely CJK characters.
    """
    n = len(text)
    if n == 0:
        return False
    alnum_space = sum(1 for ch in text if ch.isalnum() or ch.isspace())
    if alnum_space / n < _MIN_TEXTUAL_RATIO:
        return False
    cjk = sum(1 for ch in text if _is_cjk(ch))
    spaces = sum(1 for ch in text if ch.isspace())
    # Long non-CJK text with essentially no whitespace is almost always binary garbage.
    # (Short text — a title, a single word — legitimately has no spaces, so exempt it.)
    if n >= 200 and cjk / n < 0.2 and spaces / n < 0.03:
        return False
    return True


def chunk_pages(doc_id: str, pages: list[ExtractedPage], config: ChunkConfig) -> list[Passage]:
    """Turn a document's extracted pages into a flat list of passages."""
    size = max(1, config.size)
    step = max(1, size - config.overlap)

    passages: list[Passage] = []
    ordinal = 0
    for page in pages:
        page_text = clean_text(page.text)
        # Skip pages that are mostly binary garbage (failed extraction).
        if not page_text.strip() or not _is_mostly_textual(page_text):
            continue
        words = [(m.group(0), m.start()) for m in _WORD_RE.finditer(page_text)]
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
