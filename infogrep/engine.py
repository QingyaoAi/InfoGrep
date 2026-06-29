"""Search engine: the shared core behind both the CLI and the MCP server.

Owns the retrievers for one indexed directory, runs them individually or fused (RRF),
and degrades gracefully when a backend's index is missing or a backend errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .retrieval.base import Result, with_file_metadata
from .retrieval.fusion import reciprocal_rank_fusion

# Per-retriever candidate pool size for fusion (>= k so RRF has material to work with).
_POOL_MIN = 20

ALL_RETRIEVERS = ("sparse", "dense", "kb")


@dataclass
class HybridResults:
    """Fused results plus which retrievers actually contributed / were skipped."""

    results: list[Result]
    used: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # retriever -> reason


class SearchEngine:
    def __init__(self, config: Config):
        self.config = config
        self._sparse = None
        self._dense = None
        self._kb = None

    # -- lazy backends -----------------------------------------------------

    @property
    def sparse(self):
        if self._sparse is None:
            from .retrieval.sparse import SparseIndex

            self._sparse = SparseIndex(
                self.config.sparse_dir,
                self.config.cache_dir,
                field_boosts=self.config.sparse.field_boosts,
                language=self.config.sparse.language,
                prf_fb_docs=self.config.sparse.prf_fb_docs,
                prf_fb_terms=self.config.sparse.prf_fb_terms,
            )
        return self._sparse

    @property
    def dense(self):
        if self._dense is None:
            from .retrieval.dense import DenseIndex

            self._dense = DenseIndex(self.config)
        return self._dense

    @property
    def kb(self):
        if self._kb is None:
            from .retrieval.kb import KnowledgeBaseIndex

            self._kb = KnowledgeBaseIndex(self.config)
        return self._kb

    # -- individual retrievers --------------------------------------------

    def _enrich(self, results: list[Result], root) -> list[Result]:
        """Attach the original file path + metadata to each result."""
        return [with_file_metadata(r, root) for r in results]

    def search_sparse(self, query: str, k: int = 10, prf: bool = False) -> list[Result]:
        # Content-file retrievers: paths are relative to the indexed directory.
        return self._enrich(self.sparse.search(query, k=k, prf=prf), self.config.target_dir)

    def search_dense(self, query: str, k: int = 10) -> list[Result]:
        return self._enrich(self.dense.search(query, k=k), self.config.target_dir)

    def search_kb(self, query: str, k: int = 10) -> list[Result]:
        # KB paths are vault-relative; we have the vault name (CLI target), not its
        # filesystem root, so set filename/ext only (root=None leaves abs_path unset).
        return self._enrich(self.kb.search(query, k=k), None)

    def _run(self, name: str, query: str, k: int, prf: bool) -> list[Result]:
        if name == "sparse":
            return self.search_sparse(query, k=k, prf=prf)
        if name == "dense":
            return self.search_dense(query, k=k)
        if name == "kb":
            return self.search_kb(query, k=k)
        raise ValueError(f"unknown retriever: {name}")

    def _enabled(self, name: str) -> bool:
        return {
            "sparse": self.config.sparse.enabled,
            "dense": self.config.dense.enabled,
            "kb": self.config.kb.enabled,
        }.get(name, False)

    # -- fused -------------------------------------------------------------

    def search_hybrid(
        self,
        query: str,
        k: int = 10,
        retrievers: list[str] | None = None,
        prf: bool = False,
    ) -> HybridResults:
        names = retrievers or [r for r in ALL_RETRIEVERS if self._enabled(r)]
        pool = max(k, _POOL_MIN)

        lists: list[list[Result]] = []
        out = HybridResults(results=[])
        for name in names:
            if not self._enabled(name):
                out.skipped[name] = "disabled in config"
                continue
            try:
                hits = self._run(name, query, pool, prf)
            except FileNotFoundError as exc:
                out.skipped[name] = str(exc)
                continue
            except Exception as exc:  # one backend failing shouldn't sink the query
                out.skipped[name] = f"error: {exc}"
                continue
            lists.append(hits)
            out.used.append(name)

        out.results = reciprocal_rank_fusion(lists, top_n=k) if lists else []
        return out

    # -- maintenance -------------------------------------------------------

    def status(self) -> dict:
        from .indexer import Indexer

        return Indexer(self.config).status()

    def reindex(self, full: bool = False) -> dict:
        from .indexer import Indexer

        return Indexer(self.config).reindex(full=full).as_dict()
