"""HTTP integration test for the web UI (dense/hash backend -> no JVM, no torch)."""

import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from infogrep.config import Config
from infogrep.indexer import Indexer
from infogrep.web import PAGE, _make_handler

_LIGHT_CONFIG = "[sparse]\nenabled = false\n[dense]\nenabled = true\nembedder = 'hash'\n"


def _make_indexed(dirpath, files):
    dirpath.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (dirpath / name).write_text(content)
    cfg = Config.load(dirpath)
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index_dir / "config.toml").write_text(_LIGHT_CONFIG)
    Indexer(Config.load(dirpath)).reindex()
    return dirpath


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


def test_open_api_reveals_and_validates_path(tmp_path, monkeypatch):
    import infogrep.web as web

    revealed = []
    monkeypatch.setattr(web, "_reveal_in_file_manager", lambda p: revealed.append(p))
    _indexed_dir(tmp_path)
    httpd, port = _start(tmp_path)
    try:
        # A file inside the indexed dir -> revealed.
        target = str(tmp_path / "berry.txt")
        _, body = _get(port, "/api/open?path=" + urllib.parse.quote(target))
        assert json.loads(body)["ok"] is True
        assert revealed == [target]

        # A path outside the indexed dir is rejected (no reveal).
        _, body = _get(port, "/api/open?path=" + urllib.parse.quote("/etc/hosts"))
        out = json.loads(body)
        assert out["ok"] is False and "outside" in out["error"]
        assert revealed == [target]  # unchanged

        # A non-existent file under the dir is reported.
        _, body = _get(port, "/api/open?path=" + urllib.parse.quote(str(tmp_path / "nope.txt")))
        assert json.loads(body)["ok"] is False
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


def _q(port, path):
    return json.loads(_get(port, path)[1])


def test_list_indexes_and_dir_scoped_search(tmp_path):
    a = _make_indexed(tmp_path / "alpha", {"berry.txt": "blueberries rich in antioxidants"})
    b = _make_indexed(tmp_path / "beta", {"car.txt": "the sedan has a strong engine"})
    httpd, port = _start(a)  # server's default dir = alpha
    try:
        names = {i["name"] for i in _q(port, "/api/indexes")["indexes"]}
        assert {"alpha", "beta"} <= names
        # default (alpha)
        ra = _q(port, "/api/search?q=antioxidants&mode=dense")
        assert ra["results"][0]["path"] == "berry.txt"
        # dir-scoped (beta)
        rb = _q(port, "/api/search?q=engine&mode=dense&dir=" + urllib.parse.quote(str(b)))
        assert rb["results"][0]["path"] == "car.txt"
    finally:
        httpd.shutdown()


def test_index_endpoint_builds_a_new_folder(tmp_path):
    c = tmp_path / "gamma"
    c.mkdir()
    (c / "memo.txt").write_text("a uniquezorptoken appears here")
    cfg = Config.load(c)
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index_dir / "config.toml").write_text(_LIGHT_CONFIG)

    httpd, port = _start(tmp_path)
    try:
        started = _q(port, "/api/index?dir=" + urllib.parse.quote(str(c)))
        assert started["ok"]
        st = {}
        for _ in range(80):
            st = _q(port, "/api/status?dir=" + urllib.parse.quote(str(c)))
            if st.get("indexed") and not st.get("indexing"):
                break
            time.sleep(0.1)
        assert st.get("indexed") and st.get("n_files") == 1
        res = _q(port, "/api/search?q=uniquezorptoken&mode=dense&dir=" + urllib.parse.quote(str(c)))
        assert res["results"][0]["path"] == "memo.txt"
    finally:
        httpd.shutdown()
