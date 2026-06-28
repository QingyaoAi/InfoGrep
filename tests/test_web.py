"""HTTP integration test for the web UI (dense/hash backend -> no JVM, no torch)."""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from infogrep.config import Config
from infogrep.indexer import Indexer
from infogrep.web import PAGE, _make_handler


def _start(directory):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(directory))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.status, r.read().decode("utf-8")


def _indexed_dir(tmp_path):
    (tmp_path / "berry.txt").write_text("blueberries are rich in antioxidants and vitamins")
    (tmp_path / "car.txt").write_text("the sedan has a powerful engine and four wheels")
    cfg = Config.load(tmp_path)
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index_dir / "config.toml").write_text(
        "[sparse]\nenabled = false\n[dense]\nenabled = true\nembedder = 'hash'\n"
    )
    Indexer(Config.load(tmp_path)).reindex()
    return tmp_path


def test_page_served(tmp_path):
    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        status, body = _get(port, "/")
        assert status == 200
        assert "<title>InfoGrep</title>" in body
        assert body == PAGE
    finally:
        httpd.shutdown()


def test_status_api(tmp_path):
    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        _, body = _get(port, "/api/status")
        info = json.loads(body)
        assert info["indexed"] is True
        assert info["n_files"] == 2
        assert info["dir"].endswith(str(tmp_path.name))
    finally:
        httpd.shutdown()


def test_search_api(tmp_path):
    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        _, body = _get(port, "/api/search?q=antioxidants%20vitamins&mode=dense&k=2")
        data = json.loads(body)
        assert data["used"] == ["dense"]
        assert data["results"] and data["results"][0]["path"] == "berry.txt"
        assert data["results"][0]["retriever"] == "dense"

        # Empty query is a clean error, not a crash.
        _, body2 = _get(port, "/api/search?q=&mode=dense")
        assert json.loads(body2)["error"]

        # Unknown route -> 404 JSON.
        try:
            _get(port, "/nope")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()
