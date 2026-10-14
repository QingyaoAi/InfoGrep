"""MCP server exposing InfoGrep retrieval as agent tools.

Tools: search_sparse, search_dense, search_kb, search_graph, search_hybrid, read_file,
kb_learn, index_status, reindex.

The server is bound to a default directory (the indexed project root) chosen at launch;
every tool also accepts an optional ``directory`` to target a different indexed tree.
Launch via ``infogrep mcp [--dir DIR]`` (stdio transport, the form Claude Code/Codex use).

A search is handed to a running ``infogrep serve`` when there is one: it already holds
the indexes open (the JVM, the embedding model), so this process stays a thin client
instead of loading a second copy of both. With no server, the search runs here.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import Config, index_home, indexed_dirs
from .engine import ALL_RETRIEVERS, SearchEngine, search_across

# Default target dir: INFOGREP_DIR env (set by the launch command) or cwd.
_DEFAULT_DIR = os.environ.get("INFOGREP_DIR") or "."
# INFOGREP_SCOPE=all (or `--all-folders`): a tool called without a directory searches
# every indexed folder rather than the default one.
_ALL_FOLDERS = os.environ.get("INFOGREP_SCOPE") == "all"
# INFOGREP_EXCLUDE=kb: retrievers this server leaves out, of a fused search and as a tool
# of their own (`infogrep ask` skips the knowledge base this way unless asked for it).
_EXCLUDED = tuple(
    r for r in os.environ.get("INFOGREP_EXCLUDE", "").split(",") if r in ALL_RETRIEVERS
)

_SERVER_TIMEOUT_S = 120.0  # a first dense or knowledge-base query can take a while
_SERVER_RECHECK_S = 30.0  # how long "there is no usable server" stays believed
_server_checked: float | None = None
_server_usable = False
# Requests to the local server must not be routed through a system or env HTTP proxy.
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_READ_LIMIT = 20000  # characters of a file returned per read_file call

mcp = FastMCP("infogrep")

_engines: dict[str, tuple[str | None, SearchEngine]] = {}  # dir -> (index stamp, engine)


def _resolve(directory: str | None) -> str:
    return str(Path(directory or _DEFAULT_DIR).expanduser().resolve())


def _engine(directory: str | None) -> SearchEngine:
    """The engine for a directory, kept open between calls.

    A new engine per call would reopen the index, and reload the embedding model, on
    every search. One built before the index was last rewritten is replaced.
    """
    from .indexer import Indexer

    key = _resolve(directory)
    config = Config.load(key)
    stamp = Indexer(config).status(check_staleness=False).get("last_indexed_at")
    held = _engines.get(key)
    if held is None or held[0] != stamp:
        held = _engines[key] = (stamp, SearchEngine(config))
    return held[1]


def _fetch(url: str, timeout: float) -> dict:
    with _direct.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _server() -> str | None:
    """Base URL of a running ``infogrep serve`` that reads the same indexes, else None.

    ``INFOGREP_SERVER`` names it (default: the standard local port); set it empty to
    keep every search in this process.
    """
    global _server_checked, _server_usable
    from .web import DEFAULT_PORT

    base = os.environ.get("INFOGREP_SERVER", f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
    if not base:
        return None
    now = time.monotonic()
    if _server_checked is None or (not _server_usable and now - _server_checked > _SERVER_RECHECK_S):
        _server_checked = now
        try:
            info = _fetch(base + "/api/ping", timeout=1.0)
            # Same code and same index location, or its answers aren't ours to return.
            _server_usable = (
                info.get("version") == __version__
                and info.get("home") == str(index_home().resolve())
            )
        except (OSError, ValueError):
            _server_usable = False
    return base if _server_usable else None


def _search(
    mode: str,
    query: str,
    k: int,
    directory: str | None,
    prf: bool = False,
    retrievers: list[str] | None = None,
) -> dict:
    """One search, as ``{"results", "used", "skipped"}``: by the server if it's up, else here."""
    global _server_usable
    everywhere = _ALL_FOLDERS and not directory
    if everywhere:
        prf = False  # query expansion works on one index's statistics
    exclude = tuple(
        r for r in ALL_RETRIEVERS if r in _EXCLUDED or (retrievers and r not in retrievers)
    )

    base = _server()
    if base:
        params = {"q": query, "mode": mode, "k": str(k), "prf": "1" if prf else "0"}
        if everywhere:
            params["scope"] = "all"
        else:
            params["dir"] = _resolve(directory)
        if exclude:
            params["exclude"] = ",".join(exclude)
        try:
            out = _fetch(
                f"{base}/api/search?{urllib.parse.urlencode(params)}", _SERVER_TIMEOUT_S
            )
        except (OSError, ValueError):
            _server_usable = False  # it went away mid-session: search here instead
        else:
            if out.get("error"):
                raise RuntimeError(out["error"])
            return {"results": out["results"], "used": out.get("used", []),
                    "skipped": out.get("skipped", {})}

    dirs = [str(d) for d in indexed_dirs()] if everywhere else []
    if len(dirs) > 1:
        found = search_across([_engine(d) for d in dirs], mode, query, k=k, exclude=exclude)
    else:
        engine = _engine(dirs[0] if dirs else directory)
        if mode == "hybrid":
            found = engine.search_hybrid(
                query, k=k, retrievers=retrievers, prf=prf, exclude=_EXCLUDED
            )
        else:
            found = engine.search(mode, query, k=k, prf=prf)
    return {"results": [r.to_dict() for r in found.results],
            "used": found.used, "skipped": found.skipped}


@mcp.tool()
def search_sparse(query: str, k: int = 10, prf: bool = False, directory: str | None = None) -> dict:
    """Keyword/BM25 search over file contents. Best for exact terms, names, code symbols.

    Args:
        query: search query.
        k: number of results.
        prf: enable RM3 pseudo-relevance feedback (query expansion).
        directory: indexed directory to search (defaults to the server's directory).

    Returns a dict with a ``results`` list.
    """
    return {"results": _search("sparse", query, k, directory, prf=prf)["results"]}


@mcp.tool()
def search_dense(query: str, k: int = 10, directory: str | None = None) -> dict:
    """Semantic/embedding search over file contents. Best for meaning and paraphrase.

    Args:
        query: search query (natural language works well).
        k: number of results.
        directory: indexed directory to search (defaults to the server's directory).

    Returns a dict with a ``results`` list.
    """
    return {"results": _search("dense", query, k, directory)["results"]}


@mcp.tool()
def search_kb(query: str, k: int = 10, directory: str | None = None) -> dict:
    """Graph-aware search over an Obsidian knowledge-base vault.

    Matches notes by content/title/tags, then expands along ``[[wikilinks]]`` so that
    notes connected to a match are surfaced too. Reads the vault's files (the Obsidian
    app needn't be running); requires kb.enabled in the directory's config.

    Args:
        query: search query.
        k: number of results.
        directory: indexed directory whose config names the vault (defaults to server's).

    Returns a dict with a ``results`` list.
    """
    return {"results": _search("kb", query, k, directory)["results"]}


@mcp.tool()
def search_graph(query: str, k: int = 10, directory: str | None = None) -> dict:
    """Folder/filename metadata-graph search (no file content involved).

    Matches the query against folder and file *names*, then expands to neighboring
    folders (parent/children/siblings) so files that live in the most relevant
    folder(s) surface too — not just files whose own name/content matched the query.
    Built automatically on every reindex (``[graph] enabled = true``, the default).

    Args:
        query: search query (matched against folder/file names, not content).
        k: number of results.
        directory: indexed directory to search (defaults to the server's directory).

    Returns a dict with a ``results`` list.
    """
    return {"results": _search("graph", query, k, directory)["results"]}


@mcp.tool()
def search_hybrid(
    query: str,
    k: int = 10,
    retrievers: list[str] | None = None,
    prf: bool = False,
    directory: str | None = None,
) -> dict:
    """Fused search (sparse + dense + graph [+ kb]) combined with reciprocal rank fusion.

    The recommended default tool: robust across keyword and semantic intent, and also
    pulls in sibling files from the most relevant folder(s) via the metadata graph.

    Args:
        query: search query.
        k: number of results.
        retrievers: subset of ["sparse", "dense", "kb", "graph"]; defaults to all enabled.
        prf: enable RM3 PRF for the sparse component.
        directory: indexed directory to search (defaults to the server's directory).

    Returns a dict with ``results`` plus ``used``/``skipped`` retrievers.
    """
    return _search("hybrid", query, k, directory, prf=prf, retrievers=retrievers)


@lru_cache(maxsize=4)
def _file_text(path: str, mtime: float, ocr: bool, ocr_min_chars: int) -> str:
    """A file's extracted text, pages marked; kept so reading on doesn't extract again."""
    from .ingest.extract.registry import extract

    pages = extract(Path(path), ocr=ocr, ocr_min_chars=ocr_min_chars)
    return "\n\n".join(
        (f"[page {p.page}]\n" if p.page is not None else "") + p.text for p in pages
    )


@mcp.tool()
def read_file(path: str, offset: int = 0, limit: int = _READ_LIMIT) -> dict:
    """Read a file's text the way InfoGrep extracts it: PDF, Word, slides, sheets, text.

    A search result carries only a short snippet; use this to read the file behind it.
    Only files inside an indexed directory, or a knowledge-base vault, can be read.

    Args:
        path: the file to read — a result's ``abs_path``.
        offset: character to start from (0 for the beginning).
        limit: how many characters to return.

    Returns ``text`` (pages and slides are marked ``[page N]``), ``total_chars``, and
    ``next_offset`` to pass as ``offset`` for the rest (None once the end is reached).
    """
    from .retrieval.kb import vault_roots

    real = os.path.realpath(os.path.join(_resolve(None), os.path.expanduser(path)))
    roots = [os.path.realpath(str(d)) for d in [*indexed_dirs(), *vault_roots()]]
    roots.append(_resolve(None))
    root = next((r for r in roots if real == r or real.startswith(r + os.sep)), None)
    if root is None:
        raise ValueError(f"{path} is not inside an indexed directory or a vault")
    if not os.path.isfile(real):
        raise FileNotFoundError(f"no such file: {path}")
    ingest = Config.load(root).ingest
    text = _file_text(real, os.path.getmtime(real), ingest.ocr, ingest.ocr_min_chars)
    start = max(0, offset)
    end = start + max(1, limit)
    out = {
        "path": real,
        "text": text[start:end],
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }
    if not text:
        out["note"] = "no text could be extracted (a scan or an image, with OCR off?)"
    return out


@mcp.tool()
def kb_learn(
    query: str, k: int = 12, max_entities: int = 8, directory: str | None = None
) -> dict:
    """Distill what the indexed files say about ``query`` into the knowledge-base vault.

    Searches the indexed files (sparse + dense + graph), mines the top passages for
    related entities, and writes interlinked Obsidian notes into the vault's agent
    folder: a topic note (source paths + snippets + entity wikilinks) plus one note
    per entity linking back. Future ``search_kb``/``search_hybrid`` calls then answer
    questions about the topic or its entities straight from the vault via link
    expansion. Idempotent per topic: rebuilding refreshes the topic note without
    duplicating entity mentions. Writes the vault's files directly (no Obsidian app).

    Args:
        query: topic to research and persist (also becomes the topic note's title).
        k: how many passages to gather as sources.
        max_entities: cap on related-entity notes to create/link.
        directory: indexed directory to learn from (defaults to the server's).

    Returns a build summary: topic note path, entities, notes created/updated,
    and which retrievers contributed.
    """
    return _engine(directory).learn(query, k=k, max_entities=max_entities)


@mcp.tool()
def index_status(directory: str | None = None) -> dict:
    """Report index status for a directory: whether indexed, file/passage counts, last update."""
    return _engine(directory).status()


@mcp.tool()
def reindex(directory: str | None = None, full: bool = False) -> dict:
    """Build or incrementally update the index for a directory. Returns a change summary.

    Args:
        directory: directory to (re)index (defaults to the server's directory).
        full: force a full rebuild instead of an incremental update.
    """
    return _engine(directory).reindex(full=full)


# The tools that write: the index, or notes in the knowledge-base vault.
_WRITING_TOOLS = ("kb_learn", "reindex")


def main(
    directory: str | None = None, all_folders: bool = False, read_only: bool = False
) -> None:
    """Entry point used by ``infogrep mcp``; binds the default directory and serves on stdio.

    ``all_folders`` makes a tool called without a directory search every indexed
    folder; ``read_only`` leaves out the tools that write.
    """
    global _DEFAULT_DIR, _ALL_FOLDERS
    if directory:
        _DEFAULT_DIR = directory
    if all_folders:
        _ALL_FOLDERS = True
    os.environ.setdefault("INFOGREP_DIR", _DEFAULT_DIR)
    if read_only:
        for name in _WRITING_TOOLS:
            mcp.remove_tool(name)
    for name in _EXCLUDED:
        mcp.remove_tool(f"search_{name}")
    mcp.run(transport="stdio")


# Expose retriever names for clients that introspect.
__all__ = ["ALL_RETRIEVERS", "main", "mcp"]
