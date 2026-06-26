"""MCP server exposing InfoGrep retrieval as agent tools.

Tools: search_sparse, search_dense, search_hybrid, index_status, reindex.

The server is bound to a default directory (the indexed project root) chosen at launch;
every tool also accepts an optional ``directory`` to target a different indexed tree.
Launch via ``infogrep mcp [--dir DIR]`` (stdio transport, the form Claude Code/Codex use).
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import Config
from .engine import ALL_RETRIEVERS, SearchEngine

# Default target dir: INFOGREP_DIR env (set by the launch command) or cwd.
_DEFAULT_DIR = os.environ.get("INFOGREP_DIR", ".")

mcp = FastMCP("infogrep")


def _engine(directory: str | None) -> SearchEngine:
    return SearchEngine(Config.load(Path(directory or _DEFAULT_DIR)))


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
    return {"results": [r.to_dict() for r in _engine(directory).search_sparse(query, k=k, prf=prf)]}


@mcp.tool()
def search_dense(query: str, k: int = 10, directory: str | None = None) -> dict:
    """Semantic/embedding search over file contents. Best for meaning and paraphrase.

    Args:
        query: search query (natural language works well).
        k: number of results.
        directory: indexed directory to search (defaults to the server's directory).

    Returns a dict with a ``results`` list.
    """
    return {"results": [r.to_dict() for r in _engine(directory).search_dense(query, k=k)]}


@mcp.tool()
def search_kb(query: str, k: int = 10, directory: str | None = None) -> dict:
    """Graph-aware search over an Obsidian knowledge-base vault.

    Matches notes by content/title/tags, then expands along ``[[wikilinks]]`` so that
    notes connected to a match are surfaced too. Requires kb.vault_path + kb.enabled
    in the directory's config.

    Args:
        query: search query.
        k: number of results.
        directory: indexed directory whose config names the vault (defaults to server's).

    Returns a dict with a ``results`` list.
    """
    return {"results": [r.to_dict() for r in _engine(directory).search_kb(query, k=k)]}


@mcp.tool()
def search_hybrid(
    query: str,
    k: int = 10,
    retrievers: list[str] | None = None,
    prf: bool = False,
    directory: str | None = None,
) -> dict:
    """Fused search (sparse + dense [+ kb]) combined with reciprocal rank fusion.

    The recommended default tool: robust across keyword and semantic intent.

    Args:
        query: search query.
        k: number of results.
        retrievers: subset of ["sparse", "dense", "kb"]; defaults to all enabled.
        prf: enable RM3 PRF for the sparse component.
        directory: indexed directory to search (defaults to the server's directory).

    Returns a dict with ``results`` plus ``used``/``skipped`` retrievers.
    """
    out = _engine(directory).search_hybrid(query, k=k, retrievers=retrievers, prf=prf)
    return {
        "results": [r.to_dict() for r in out.results],
        "used": out.used,
        "skipped": out.skipped,
    }


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


def main(directory: str | None = None) -> None:
    """Entry point used by ``infogrep mcp``; binds the default directory and serves on stdio."""
    global _DEFAULT_DIR
    if directory:
        _DEFAULT_DIR = directory
    os.environ.setdefault("INFOGREP_DIR", _DEFAULT_DIR)
    mcp.run(transport="stdio")


# Expose retriever names for clients that introspect.
__all__ = ["mcp", "main", "ALL_RETRIEVERS"]
