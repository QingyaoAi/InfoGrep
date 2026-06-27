"""Core retrieval types shared by every backend.

Every retriever (sparse, dense, knowledge base) returns a list of :class:`Result`,
so the fusion layer and MCP server can treat them uniformly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Result:
    """A single retrieved passage with enough provenance for an agent to cite it.

    ``path`` is relative to the search root; ``abs_path`` and the file metadata
    (``filename``/``ext``/``size``/``mtime``) are filled in by :func:`with_file_metadata`
    so callers get the original file location, not just a relative passage reference.
    """

    doc_id: str
    passage_id: str
    path: str
    snippet: str
    score: float
    retriever: str  # "sparse" | "dense" | "kb"
    page: int | None = None
    offset: int | None = None
    # File-level metadata (populated at result time, not stored in the index).
    abs_path: str | None = None
    filename: str | None = None
    ext: str | None = None
    size: int | None = None
    mtime: float | None = None

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "passage_id": self.passage_id,
            "path": self.path,
            "abs_path": self.abs_path,
            "filename": self.filename,
            "ext": self.ext,
            "size": self.size,
            "mtime": self.mtime,
            "snippet": self.snippet,
            "score": self.score,
            "retriever": self.retriever,
            "page": self.page,
            "offset": self.offset,
        }


def with_file_metadata(result: "Result", root: os.PathLike | str | None) -> "Result":
    """Return a copy of ``result`` enriched with the original file path + metadata.

    ``root`` is the filesystem root the result's ``path`` is relative to (the indexed
    directory, or a vault). Filename/extension come from the path regardless; the
    absolute path and size/mtime are added when ``root`` is given and the file exists.
    """
    filename = os.path.basename(result.path)
    ext = (os.path.splitext(filename)[1].lstrip(".").lower() or None)
    abs_path = size = mtime = None
    if root is not None:
        abs_path = os.path.join(str(root), result.path)
        try:
            st = os.stat(abs_path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            pass
    return replace(
        result, abs_path=abs_path, filename=filename, ext=ext, size=size, mtime=mtime
    )


@runtime_checkable
class Retriever(Protocol):
    """Common interface for all retrieval backends."""

    name: str

    def search(self, query: str, k: int = 10) -> list[Result]:
        """Return up to ``k`` results for ``query``, ranked best-first."""
        ...
