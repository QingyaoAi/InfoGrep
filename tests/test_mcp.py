"""Smoke tests for the MCP tool layer.

These call the tool functions directly (the FastMCP decorator keeps the wrapped
callable invokable), exercising the same code path the agent transport would.
"""

from infogrep.config import Config
from infogrep.indexer import Indexer


def _index(tmp_path):
    (tmp_path / "fox.txt").write_text("The quick brown fox jumps over the lazy dog.")
    (tmp_path / "berry.txt").write_text("Blueberries are rich in antioxidants and vitamins.")
    # Persist config so the MCP tools (which reload config from disk) agree with how
    # the index was built: hash embedder, sparse off (JVM-free).
    sidecar = tmp_path / ".infogrep"
    sidecar.mkdir(exist_ok=True)
    (sidecar / "config.toml").write_text(
        "[sparse]\nenabled = false\n[dense]\nembedder = 'hash'\n"
    )
    Indexer(Config.load(tmp_path)).reindex()


def test_tools_registered():
    import infogrep.mcp_server as srv

    # FastMCP exposes the registered tools; all five should be present.
    names = {"search_sparse", "search_dense", "search_hybrid", "index_status", "reindex"}
    assert names.issubset(set(dir(srv)))


def test_search_dense_tool_returns_dicts(tmp_path):
    _index(tmp_path)
    from infogrep.mcp_server import search_dense

    out = search_dense("antioxidants vitamins", k=2, directory=str(tmp_path))
    assert set(out) == {"results"}
    hits = out["results"]
    assert hits and hits[0]["path"] == "berry.txt"
    assert hits[0]["retriever"] == "dense"
    assert set(["path", "snippet", "score", "page", "retriever"]).issubset(hits[0])


def test_index_status_tool(tmp_path):
    _index(tmp_path)
    from infogrep.mcp_server import index_status

    info = index_status(directory=str(tmp_path))
    assert info["indexed"] is True
    assert info["n_files"] == 2


def test_search_hybrid_tool_shape(tmp_path):
    _index(tmp_path)
    from infogrep.mcp_server import search_hybrid

    out = search_hybrid("blueberries", k=2, directory=str(tmp_path))
    assert set(out) == {"results", "used", "skipped"}
    assert out["used"] == ["dense"]  # sparse disabled in config -> not in the default set
    assert out["results"] and out["results"][0]["path"] == "berry.txt"
