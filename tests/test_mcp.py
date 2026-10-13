"""Smoke tests for the MCP tool layer.

These call the tool functions directly (the FastMCP decorator keeps the wrapped
callable invokable), exercising the same code path the agent transport would.
"""

from infogrep.config import Config
from infogrep.indexer import Indexer


def _index(tmp_path):
    (tmp_path / "fox.txt").write_text("The quick brown fox jumps over the lazy dog.")
    (tmp_path / "berry.txt").write_text("Blueberries are rich in antioxidants and vitamins.")
    # Persist config (in the separate index dir) so the MCP tools, which reload config
    # from disk, agree with how the index was built: hash embedder, sparse off (JVM-free).
    cfg = Config.load(tmp_path)
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index_dir / "config.toml").write_text(
        "[sparse]\nenabled = false\n[dense]\nenabled = true\nembedder = 'hash'\n"
        "[graph]\nenabled = false\n"
    )
    Indexer(Config.load(tmp_path)).reindex()


def test_tools_registered():
    import infogrep.mcp_server as srv

    # FastMCP exposes the registered tools; all five should be present.
    names = {
        "search_sparse", "search_dense", "search_kb", "search_graph",
        "search_hybrid", "index_status", "reindex",
    }
    assert names.issubset(set(dir(srv)))


def test_search_dense_tool_returns_dicts(tmp_path):
    _index(tmp_path)
    from infogrep.mcp_server import search_dense

    out = search_dense("antioxidants vitamins", k=2, directory=str(tmp_path))
    assert set(out) == {"results"}
    hits = out["results"]
    assert hits and hits[0]["path"] == "berry.txt"
    assert hits[0]["retriever"] == "dense"
    assert {"path", "snippet", "score", "page", "retriever"}.issubset(hits[0])


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


def test_engine_is_kept_between_calls_until_the_index_is_rewritten(tmp_path):
    _index(tmp_path)
    import infogrep.mcp_server as srv

    first = srv._engine(str(tmp_path))
    assert srv._engine(str(tmp_path)) is first  # no reopening (or model reload) per call

    (tmp_path / "car.txt").write_text("The sedan has a powerful engine and four wheels.")
    Indexer(Config.load(tmp_path)).reindex()
    assert srv._engine(str(tmp_path)) is not first  # it would be searching the old index
    assert srv.search_dense("sedan engine", k=1, directory=str(tmp_path))["results"]


def test_read_file_returns_the_text_in_pieces(tmp_path):
    _index(tmp_path)
    from infogrep.mcp_server import read_file

    whole = read_file(str(tmp_path / "fox.txt"))
    assert whole["text"] == "The quick brown fox jumps over the lazy dog."
    assert whole["total_chars"] == 44 and whole["next_offset"] is None

    head = read_file(str(tmp_path / "fox.txt"), limit=9)
    assert head["text"] == "The quick" and head["next_offset"] == 9
    assert read_file(str(tmp_path / "fox.txt"), offset=9, limit=6)["text"] == " brown"


def test_read_file_stays_inside_indexed_directories(tmp_path, tmp_path_factory):
    import pytest

    _index(tmp_path)
    from infogrep.mcp_server import read_file

    elsewhere = tmp_path_factory.mktemp("private") / "secret.txt"
    elsewhere.write_text("not indexed")
    with pytest.raises(ValueError, match="not inside an indexed directory"):
        read_file(str(elsewhere))
    with pytest.raises(FileNotFoundError):
        read_file(str(tmp_path / "gone.txt"))


def test_read_only_leaves_out_the_tools_that_write(monkeypatch):
    import infogrep.mcp_server as srv

    removed = []

    class Recorder:
        def remove_tool(self, name):
            removed.append(name)

        def run(self, transport):
            pass

    monkeypatch.setattr(srv, "mcp", Recorder())
    monkeypatch.delenv("INFOGREP_DIR", raising=False)  # main() sets it; undone after the test
    srv.main(read_only=True)
    assert removed == ["kb_learn", "reindex"]


def test_an_excluded_retriever_is_left_out_of_searches_and_of_the_tools(tmp_path, monkeypatch):
    _index(tmp_path)  # dense is the only retriever this index has
    import infogrep.mcp_server as srv

    assert srv.search_hybrid("blueberries", k=2, directory=str(tmp_path))["used"] == ["dense"]
    monkeypatch.setattr(srv, "_EXCLUDED", ("dense",))
    out = srv.search_hybrid("blueberries", k=2, directory=str(tmp_path))
    assert out == {"results": [], "used": [], "skipped": {}}

    httpd = _serve(tmp_path)  # ...and the same when the search is handed to a server
    try:
        monkeypatch.setenv("INFOGREP_SERVER", f"http://127.0.0.1:{httpd.server_address[1]}")
        monkeypatch.setattr(srv, "_server_checked", None)
        assert srv.search_hybrid("blueberries", k=2, directory=str(tmp_path))["used"] == []
    finally:
        httpd.shutdown()

    removed = []

    class Recorder:
        def remove_tool(self, name):
            removed.append(name)

        def run(self, transport):
            pass

    monkeypatch.setattr(srv, "mcp", Recorder())
    monkeypatch.setattr(srv, "_EXCLUDED", ("kb",))
    monkeypatch.delenv("INFOGREP_DIR", raising=False)
    srv.main()
    assert removed == ["search_kb"]


def _serve(directory):
    import threading
    from http.server import ThreadingHTTPServer

    from infogrep.web import _make_handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(directory))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_searches_are_handed_to_a_running_server(tmp_path, monkeypatch):
    _index(tmp_path)
    import infogrep.mcp_server as srv

    def no_local_engine(directory):
        raise AssertionError("searched in the MCP process")

    httpd = _serve(tmp_path)
    try:
        monkeypatch.setenv("INFOGREP_SERVER", f"http://127.0.0.1:{httpd.server_address[1]}")
        monkeypatch.setattr(srv, "_server_checked", None)
        monkeypatch.setattr(srv, "_engine", no_local_engine)

        out = srv.search_hybrid("blueberries", k=2, directory=str(tmp_path))
        assert out["used"] == ["dense"] and out["results"][0]["path"] == "berry.txt"

        # With no directory named, `--all-folders` searches every indexed one.
        monkeypatch.setattr(srv, "_ALL_FOLDERS", True)
        monkeypatch.setattr(srv, "_DEFAULT_DIR", str(tmp_path / "not-indexed"))
        hits = srv.search_dense("quick brown fox", k=1)["results"]
        assert hits[0]["path"] == "fox.txt"
    finally:
        httpd.shutdown()


def test_a_server_on_other_indexes_is_not_used(tmp_path, monkeypatch):
    _index(tmp_path)
    import infogrep.mcp_server as srv
    from infogrep import __version__

    monkeypatch.setenv("INFOGREP_SERVER", "http://127.0.0.1:1")
    monkeypatch.setattr(srv, "_server_checked", None)
    monkeypatch.setattr(srv, "_fetch", lambda url, timeout: {"version": __version__, "home": "/elsewhere"})
    assert srv._server() is None
    assert srv.search_dense("antioxidants", k=1, directory=str(tmp_path))["results"]


def test_search_runs_here_when_the_server_goes_away(tmp_path, monkeypatch):
    _index(tmp_path)
    import infogrep.mcp_server as srv

    httpd = _serve(tmp_path)
    monkeypatch.setenv("INFOGREP_SERVER", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setattr(srv, "_server_checked", None)
    assert srv._server() is not None
    httpd.shutdown()
    httpd.server_close()

    hits = srv.search_dense("antioxidants vitamins", k=1, directory=str(tmp_path))["results"]
    assert hits[0]["path"] == "berry.txt"
    assert srv._server_usable is False
