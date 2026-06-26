"""Extractor registry: dispatch a file to the right text extractor by type.

Each extractor maps a file path to a list of :class:`ExtractedPage`. Dispatch is by
lowercase suffix; anything unregistered falls back to a UTF-8 text reader, and files
that don't decode as text are skipped (empty list).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..types import ExtractedPage

Extractor = Callable[[Path], list[ExtractedPage]]

# Extensions treated as plain UTF-8 text (docs + common code/config formats).
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".htm", ".xml", ".tex",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cc", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".sql",
    ".css", ".scss", ".swift", ".kt", ".scala", ".r", ".m", ".pl",
}


def _extract_text(path: Path) -> list[ExtractedPage]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    return [ExtractedPage(page=None, text=text)] if text.strip() else []


def _extract_pdf(path: Path, ocr: bool = False, ocr_min_chars: int = 16) -> list[ExtractedPage]:
    import fitz  # pymupdf

    pages: list[ExtractedPage] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text")
            # Scanned pages have little/no extractable text; OCR them if asked.
            if ocr and len(text.strip()) < ocr_min_chars:
                try:
                    tp = page.get_textpage_ocr(flags=0, full=True)
                    text = page.get_text("text", textpage=tp) or text
                except Exception:
                    pass  # tesseract missing/failed -> keep whatever we had
            if text.strip():
                pages.append(ExtractedPage(page=i, text=text))
    return pages


def _extract_docx(path: Path) -> list[ExtractedPage]:
    import docx  # python-docx

    document = docx.Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    # DOCX has no fixed pagination; emit one flow page.
    text = "\n".join(parts)
    return [ExtractedPage(page=None, text=text)] if text.strip() else []


def _extract_pptx(path: Path) -> list[ExtractedPage]:
    from pptx import Presentation  # python-pptx

    prs = Presentation(str(path))
    pages: list[ExtractedPage] = []
    for i, slide in enumerate(prs.slides, start=1):
        chunks = [
            shape.text for shape in slide.shapes if shape.has_text_frame and shape.text.strip()
        ]
        text = "\n".join(chunks)
        if text.strip():
            pages.append(ExtractedPage(page=i, text=text))
    return pages


_REGISTRY: dict[str, Extractor] = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".pptx": _extract_pptx,
}


def get_extractor(path: Path) -> Extractor:
    """Return the extractor for ``path`` (text reader as the default fallback)."""
    return _REGISTRY.get(path.suffix.lower(), _extract_text)


def is_supported(path: Path) -> bool:
    """Whether we have a content extractor for this file type."""
    suffix = path.suffix.lower()
    return suffix in _REGISTRY or suffix in _TEXT_SUFFIXES


def extract(path: Path, ocr: bool = False, ocr_min_chars: int = 16) -> list[ExtractedPage]:
    """Extract text pages from ``path`` using the registered extractor."""
    if path.suffix.lower() == ".pdf":
        return _extract_pdf(path, ocr=ocr, ocr_min_chars=ocr_min_chars)
    return get_extractor(path)(path)
