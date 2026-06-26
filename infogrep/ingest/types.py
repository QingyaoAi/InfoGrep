"""Shared ingestion data types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedPage:
    """One unit of extracted text from a file.

    ``page`` is a 1-based page/slide number when the format has them (PDF, PPTX),
    or ``None`` for flow formats (TXT, MD, DOCX).
    """

    page: int | None
    text: str


@dataclass(frozen=True)
class Passage:
    """A chunk of a document, ready to be indexed and cited."""

    passage_id: str  # f"{doc_id}#{ordinal}"
    doc_id: str  # relative posix path of the source file
    path: str  # relative posix path (== doc_id; kept explicit for clarity)
    ordinal: int  # passage index within the document, 0-based
    text: str
    page: int | None
    offset: int  # character offset of this chunk within its source page
