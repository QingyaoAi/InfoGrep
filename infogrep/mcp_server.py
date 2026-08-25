"""MCP server exposing InfoGrep retrieval as agent tools.

Tools: search_sparse, search_dense, search_kb, search_graph, search_hybrid,
kb_learn, index_status, reindex.

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
    return {"results": [r.to_dict() for r in _engine(directory).search_graph(query, k=k)]}


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
    out = _engine(directory).search_hybrid(query, k=k, retrievers=retrievers, prf=prf)
    return {
        "results": [r.to_dict() for r in out.results],
        "used": out.used,
        "skipped": out.skipped,
    }


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
    duplicating entity mentions. Requires the Obsidian app + CLI.

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


def main(directory: str | None = None) -> None:
    """Entry point used by ``infogrep mcp``; binds the default directory and serves on stdio."""
    global _DEFAULT_DIR
    if directory:
        _DEFAULT_DIR = directory
    os.environ.setdefault("INFOGREP_DIR", _DEFAULT_DIR)
    mcp.run(transport="stdio")


# Expose retriever names for clients that introspect.
__all__ = ["ALL_RETRIEVERS", "main", "mcp"]
