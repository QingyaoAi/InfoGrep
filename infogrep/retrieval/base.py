"""Core retrieval types shared by every backend.

Every retriever (sparse, dense, knowledge base) returns a list of :class:`Result`,
so the fusion layer and MCP server can treat them uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Result:
    """A single retrieved passage with enough provenance for an agent to cite it."""

    doc_id: str
    passage_id: str
    path: str
    snippet: str
    score: float
    retriever: str  # "sparse" | "dense" | "kb"
    page: int | None = None
    offset: int | None = None

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "passage_id": self.passage_id,
            "path": self.path,
            "snippet": self.snippet,
            "score": self.score,
            "retriever": self.retriever,
            "page": self.page,
            "offset": self.offset,
        }


@runtime_checkable
class Retriever(Protocol):
    """Common interface for all retrieval backends."""

    name: str

    def search(self, query: str, k: int = 10) -> list[Result]:
        """Return up to ``k`` results for ``query``, ranked best-first."""
        ...
