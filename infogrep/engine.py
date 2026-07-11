"""Search engine: the shared core behind the CLI, web UI, and MCP server.

Owns the retrievers for one indexed directory, runs them individually or fused (RRF),
and degrades gracefully when a backend's index is missing or a backend errors.

Adding a retriever backend = one factory entry in ``_FACTORIES`` (plus a config section
of the same name with an ``enabled`` flag). The CLI, web UI, and hybrid fusion all
derive their mode lists from ``ALL_RETRIEVERS``, so nothing else needs to change.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .config import Config
from .retrieval.base import Result, with_file_metadata
from .retrieval.fusion import reciprocal_rank_fusion

# Per-retriever candidate pool size for fusion (>= k so RRF has material to work with).
_POOL_MIN = 20

ALL_RETRIEVERS = ("sparse", "dense", "kb", "graph")
MODES = ("hybrid",) + ALL_RETRIEVERS


def _make_sparse(config: Config):
    from .retrieval.sparse import SparseIndex

    return SparseIndex(
        config.sparse_dir,
        config.cache_dir,
        field_boosts=config.sparse.field_boosts,
        language=config.sparse.language,
        prf_fb_docs=config.sparse.prf_fb_docs,
        prf_fb_terms=config.sparse.prf_fb_terms,
    )


def _make_dense(config: Config):
    from .retrieval.dense import DenseIndex

    return DenseIndex(config)


def _make_kb(config: Config):
    from .retrieval.kb import KnowledgeBaseIndex

    return KnowledgeBaseIndex(config)


def _make_graph(config: Config):
    from .retrieval.graph import FolderGraphIndex

    return FolderGraphIndex(
        config.index_dir, hops=config.graph.hops, max_folders=config.graph.max_folders
    )


_FACTORIES = {
    "sparse": _make_sparse,
    "dense": _make_dense,
    "kb": _make_kb,
    "graph": _make_graph,
}


@dataclass
class HybridResults:
    """Fused results plus which retrievers actually contributed / were skipped."""

    results: list[Result]
    used: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # retriever -> reason


class SearchEngine:
    def __init__(self, config: Config):
        self.config = config
        self._backends: dict[str, object] = {}
        self._backends_lock = threading.Lock()

    # -- lazy backends -----------------------------------------------------

    def _backend(self, name: str):
        with self._backends_lock:
            backend = self._backends.get(name)
            if backend is None:
                backend = self._backends[name] = _FACTORIES[name](self.config)
            return backend

    @property
    def sparse(self):
        return self._backend("sparse")

    @property
    def dense(self):
        return self._backend("dense")

    @property
    def kb(self):
        return self._backend("kb")

    @property
    def graph(self):
        return self._backend("graph")

    # -- individual retrievers --------------------------------------------

    def _run(self, name: str, query: str, k: int, prf: bool = False) -> list[Result]:
        if name not in _FACTORIES:
            raise ValueError(f"unknown retriever: {name}")
        kwargs = {"prf": prf} if name == "sparse" else {}
        hits = self._backend(name).search(query, k=k, **kwargs)
        # KB paths are vault-relative and we only know the vault's name, not its
        # filesystem root, so enrich with filename/ext only (root=None leaves abs_path
        # unset). All other retrievers' paths are relative to the indexed directory.
        root = None if name == "kb" else self.config.target_dir
        return [with_file_metadata(r, root) for r in hits]

    def search_sparse(self, query: str, k: int = 10, prf: bool = False) -> list[Result]:
        return self._run("sparse", query, k, prf=prf)

    def search_dense(self, query: str, k: int = 10) -> list[Result]:
        return self._run("dense", query, k)

    def search_kb(self, query: str, k: int = 10) -> list[Result]:
        return self._run("kb", query, k)

    def search_graph(self, query: str, k: int = 10) -> list[Result]:
        return self._run("graph", query, k)

    def _enabled(self, name: str) -> bool:
        section = getattr(self.config, name, None)
        return bool(section is not None and getattr(section, "enabled", False))

    # -- unified entry point -------------------------------------------------

    def search(self, mode: str, query: str, k: int = 10, prf: bool = False) -> HybridResults:
        """Run one search ``mode`` ("hybrid" or a retriever name).

        The single dispatch point shared by the CLI and web UI. Raises ``ValueError``
        for an unknown mode and ``FileNotFoundError`` when a single retriever's index
        is missing (hybrid instead skips missing backends).
        """
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        if mode == "hybrid":
            return self.search_hybrid(query, k=k, prf=prf)
        return HybridResults(results=self._run(mode, query, k, prf=prf), used=[mode])

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

        out = HybridResults(results=[])
        runnable = []
        for name in names:
            if self._enabled(name):
                runnable.append(name)
            else:
                out.skipped[name] = "disabled in config"

        hits_by_name: dict[str, list[Result]] = {}

        def run(name: str) -> None:
            try:
                hits_by_name[name] = self._run(name, query, pool, prf=prf)
            except FileNotFoundError as exc:
                out.skipped[name] = str(exc)
            except Exception as exc:  # one backend failing shouldn't sink the query
                out.skipped[name] = f"error: {exc}"

        # Fan out concurrently so hybrid latency is the slowest backend, not the sum.
        # Sparse stays on the caller's thread: jnius attaches native threads to the
        # JVM and never detaches pool threads, so JVM work is kept off worker threads.
        threads = [
            threading.Thread(target=run, args=(name,), daemon=True)
            for name in runnable
            if name != "sparse"
        ]
        for t in threads:
            t.start()
        if "sparse" in runnable:
            run("sparse")
        for t in threads:
            t.join()

        # Assemble in declaration order so used/skipped and fusion are deterministic.
        lists = [hits_by_name[name] for name in runnable if name in hits_by_name]
        out.used = [name for name in runnable if name in hits_by_name]
        out.results = reciprocal_rank_fusion(lists, top_n=k) if lists else []
        return out

    # -- maintenance -------------------------------------------------------

    def status(self, check_staleness: bool = True) -> dict:
        from .indexer import Indexer

        return Indexer(self.config).status(check_staleness=check_staleness)

    def reindex(self, full: bool = False) -> dict:
        from .indexer import Indexer

        return Indexer(self.config).reindex(full=full).as_dict()
