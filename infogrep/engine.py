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

from .config import DEFAULT_IMAGE_TYPES, Config
from .retrieval.base import Result, with_file_metadata
from .retrieval.fusion import reciprocal_rank_fusion

# Per-retriever candidate pool size for fusion (>= k so RRF has material to work with).
_POOL_MIN = 20

ALL_RETRIEVERS = ("sparse", "dense", "kb", "graph")
MODES = ("hybrid",) + ALL_RETRIEVERS

# File-type filters the UIs offer: id -> (label, extensions). Between them they cover
# every extension indexed by default; anything else is only reachable unfiltered.
FILE_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "pdf": ("PDF", ("pdf",)),
    "doc": ("Word", ("doc", "docx", "rtf", "odt")),
    "slides": ("Slides", ("ppt", "pptx", "odp")),
    "sheet": ("Sheets", ("xls", "xlsx", "ods", "csv", "tsv")),
    "text": ("Text", ("txt", "md", "markdown", "rst", "tex")),
    "image": ("Images", tuple(DEFAULT_IMAGE_TYPES)),
    "data": ("JSON", ("json", "jsonl")),
}


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


def _fan_out(names: list[str], run) -> None:
    """Call ``run(name)`` for every retriever, concurrently.

    Hybrid latency is then the slowest backend, not the sum. Sparse stays on the
    caller's thread: jnius attaches native threads to the JVM and never detaches pool
    threads, so JVM work is kept off worker threads.
    """
    threads = [
        threading.Thread(target=run, args=(name,), daemon=True)
        for name in names
        if name != "sparse"
    ]
    for t in threads:
        t.start()
    if "sparse" in names:
        run("sparse")
    for t in threads:
        t.join()


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

    def _run(
        self, name: str, query: str, k: int, prf: bool = False, exts: tuple = ()
    ) -> list[Result]:
        if name not in _FACTORIES:
            raise ValueError(f"unknown retriever: {name}")
        kwargs = {"prf": prf} if name == "sparse" else {}
        if exts:  # only files with these extensions (see Retriever.search)
            kwargs["exts"] = exts
        backend = self._backend(name)
        hits = backend.search(query, k=k, **kwargs)
        # KB paths are relative to the vault; every other retriever's to the indexed folder.
        root = backend.root if name == "kb" else self.config.target_dir
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

    def search(
        self,
        mode: str,
        query: str,
        k: int = 10,
        prf: bool = False,
        exclude: tuple = (),
        exts: tuple = (),
    ) -> HybridResults:
        """Run one search ``mode`` ("hybrid" or a retriever name).

        The single dispatch point shared by the CLI and web UI. Raises ``ValueError``
        for an unknown mode and ``FileNotFoundError`` when a single retriever's index
        is missing (hybrid instead skips missing backends). ``exclude`` names
        retrievers to leave out of a hybrid search; ``exts`` restricts the results to
        files with those extensions (e.g. one of ``FILE_TYPES``).
        """
        if mode not in MODES:
            raise ValueError(f"unknown mode: {mode}")
        if mode == "hybrid":
            return self.search_hybrid(query, k=k, prf=prf, exclude=exclude, exts=exts)
        results = self._run(mode, query, k, prf=prf, exts=exts)
        return HybridResults(results=results, used=[mode])

    # -- fused -------------------------------------------------------------

    def search_hybrid(
        self,
        query: str,
        k: int = 10,
        retrievers: list[str] | None = None,
        prf: bool = False,
        exclude: tuple = (),
        exts: tuple = (),
    ) -> HybridResults:
        names = retrievers or [r for r in ALL_RETRIEVERS if self._enabled(r)]
        # Left out at the caller's request (e.g. the launcher leaves out the knowledge base
        # unless asked for it), so unlike a disabled backend it isn't reported as skipped.
        names = [name for name in names if name not in exclude]
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
                hits_by_name[name] = self._run(name, query, pool, prf=prf, exts=exts)
            except FileNotFoundError as exc:
                out.skipped[name] = str(exc)
            except Exception as exc:  # one backend failing shouldn't sink the query
                out.skipped[name] = f"error: {exc}"

        _fan_out(runnable, run)

        # Assemble in declaration order so used/skipped and fusion are deterministic.
        lists = [hits_by_name[name] for name in runnable if name in hits_by_name]
        out.used = [name for name in runnable if name in hits_by_name]
        out.results = reciprocal_rank_fusion(lists, top_n=k) if lists else []
        return out

    # -- knowledge-base building --------------------------------------------

    def learn(self, query: str, k: int = 12, max_entities: int = 8) -> dict:
        """Distill search results for ``query`` into linked notes in the KB vault.

        See :mod:`infogrep.kb_builder`; returns its build summary.
        """
        from .kb_builder import KnowledgeGraphBuilder

        return KnowledgeGraphBuilder(self).build(query, k=k, max_entities=max_entities)

    # -- maintenance -------------------------------------------------------

    def warm(self) -> None:
        """Pay the one-time search costs now instead of on the first real query.

        A cold sparse query spends about a second booting the JVM and opening the
        index, the graph retriever parses its JSON on first use, and the knowledge base
        reads its vault. UIs call this when they open so that work overlaps with the user
        typing. Best-effort: a backend with no index (or vault) yet is simply left cold.
        """
        for name in ("sparse", "graph", "kb"):
            if self._enabled(name):
                try:
                    self._run(name, "warm", 1)
                except Exception:
                    pass

    def status(self, check_staleness: bool = True) -> dict:
        from .indexer import Indexer

        return Indexer(self.config).status(check_staleness=check_staleness)

    def reindex(self, full: bool = False) -> dict:
        from .indexer import Indexer

        return Indexer(self.config).reindex(full=full).as_dict()


def search_across(
    engines: list[SearchEngine],
    mode: str,
    query: str,
    k: int = 10,
    exclude: tuple = (),
    exts: tuple = (),
) -> HybridResults:
    """One search over several indexed directories, ranked as a single collection.

    Each retriever's hits are pooled across the directories by score, then fused across
    retrievers as usual. Pooling by score is only fair where a score means the same
    thing in every directory: true for the graph, dense and kb retrievers, not for
    BM25, so sparse goes through one shared-statistics search instead (see
    :func:`infogrep.retrieval.sparse.search_pooled`). Query expansion works on one
    index's statistics and is not offered here.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    if len(engines) == 1:
        return engines[0].search(mode, query, k=k, exclude=exclude, exts=exts)
    hybrid = mode == "hybrid"
    names = [r for r in ALL_RETRIEVERS if r not in exclude] if hybrid else [mode]
    pool = max(k, _POOL_MIN) if hybrid else k

    hits_by_name: dict[str, list[Result]] = {}
    failures: dict[str, Exception] = {}

    def run(name: str) -> None:
        # A retriever asked for by name runs everywhere; hybrid only where it's enabled.
        members = [e for e in engines if not hybrid or e._enabled(name)]
        lists: list[list[Result]] = []
        if name == "sparse" and members:
            try:
                lists.append(_sparse_across(members, query, pool, exts))
            except Exception as exc:
                failures[name] = exc
        else:
            for engine in members:
                try:
                    lists.append(engine._run(name, query, pool, exts=exts))
                except Exception as exc:  # one directory failing shouldn't sink the rest
                    failures.setdefault(name, exc)
        if lists:
            failures.pop(name, None)
            hits_by_name[name] = _pool_by_score(lists, pool)

    _fan_out(names, run)

    if not hybrid:
        if mode in failures:
            raise failures[mode]
        return HybridResults(results=hits_by_name.get(mode, [])[:k], used=[mode])
    out = HybridResults(results=[])
    for name, exc in failures.items():
        out.skipped[name] = str(exc) if isinstance(exc, FileNotFoundError) else f"error: {exc}"
    out.used = [name for name in names if name in hits_by_name]
    lists = [hits_by_name[name] for name in out.used]
    out.results = reciprocal_rank_fusion(lists, top_n=k) if lists else []
    return out


def _sparse_across(
    engines: list[SearchEngine], query: str, k: int, exts: tuple = ()
) -> list[Result]:
    from .retrieval.sparse import search_pooled

    root_of = {id(engine.sparse): engine.config.target_dir for engine in engines}
    hits = search_pooled([engine.sparse for engine in engines], query, k, exts=exts)
    return [with_file_metadata(result, root_of[id(index)]) for index, result in hits]


def _pool_by_score(lists: list[list[Result]], k: int) -> list[Result]:
    """Merge one retriever's hits from several directories, best score first.

    Directories sharing an Obsidian vault return the same notes; keep one of each.
    """
    seen: set[tuple] = set()
    pooled: list[Result] = []
    for result in sorted((r for hits in lists for r in hits), key=lambda r: -r.score):
        key = (result.abs_path or result.path, result.passage_id)
        if key not in seen:
            seen.add(key)
            pooled.append(result)
    return pooled[:k]
