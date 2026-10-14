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
        # Staleness (a full folder walk, slow on big trees) is opt-in via &stale=1.
        assert "stale" not in info
        _, body = _get(port, "/api/status?stale=1")
        info = json.loads(body)
        assert info["stale"] is False and info["pending"] == 0
    finally:
        httpd.shutdown()


def test_open_api_reveals_and_validates_path(tmp_path, monkeypatch):
    from infogrep import web

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


def test_warm_endpoint_is_best_effort(tmp_path):
    indexed = _make_indexed(tmp_path / "alpha", {"berry.txt": "blueberries"})
    (tmp_path / "empty").mkdir()  # default config, nothing indexed
    httpd, port = _start(indexed)
    try:
        assert _q(port, "/api/warm") == {"ok": True}
        # A folder with no index has nothing to open — still ok, never an error.
        empty = urllib.parse.quote(str(tmp_path / "empty"))
        assert _q(port, "/api/warm?dir=" + empty) == {"ok": True}
        # ...and search still works afterwards.
        assert _q(port, "/api/search?q=blueberries&mode=dense&k=1")["results"]
    finally:
        httpd.shutdown()


def test_list_indexes_and_dir_scoped_search(tmp_path):
    a = _make_indexed(tmp_path / "alpha", {"berry.txt": "blueberries rich in antioxidants"})
    b = _make_indexed(tmp_path / "beta", {"car.txt": "the sedan has a strong engine"})
    httpd, port = _start(a)  # server's default dir = alpha
    try:
        indexes = {i["name"]: i for i in _q(port, "/api/indexes")["indexes"]}
        assert {"alpha", "beta"} <= set(indexes)
        assert indexes["alpha"]["kb"] is False  # no Obsidian vault configured here
        # hybrid can leave retrievers out on request
        assert "dense" in _q(port, "/api/search?q=antioxidants&mode=hybrid")["used"]
        only_rest = _q(port, "/api/search?q=antioxidants&mode=hybrid&exclude=dense")
        assert "dense" not in only_rest["used"] and only_rest["skipped"] == {}
        # default (alpha)
        ra = _q(port, "/api/search?q=antioxidants&mode=dense")
        assert ra["results"][0]["path"] == "berry.txt"
        # dir-scoped (beta)
        rb = _q(port, "/api/search?q=engine&mode=dense&dir=" + urllib.parse.quote(str(b)))
        assert rb["results"][0]["path"] == "car.txt"
        # scope=all: one search over every indexed folder
        for query, expected in (("antioxidants", a / "berry.txt"), ("engine", b / "car.txt")):
            hit = _q(port, f"/api/search?q={query}&mode=dense&scope=all&k=1")["results"][0]
            assert hit["abs_path"] == str(expected)
        assert _q(port, "/api/warm?scope=all") == {"ok": True}
        assert "one folder" in _q(port, "/api/search?q=engine&scope=all&prf=1")["error"]
        # type=<id>: only files of one kind; the ids come with the index list
        types = {t["id"]: t["label"] for t in _q(port, "/api/indexes")["types"]}
        assert types["pdf"] == "PDF" and "text" in types
        assert _q(port, "/api/search?q=engine&mode=dense&scope=all&type=text")["results"]
        assert _q(port, "/api/search?q=engine&mode=dense&scope=all&type=pdf")["results"] == []
        assert "unknown file type" in _q(port, "/api/search?q=engine&type=zip")["error"]
    finally:
        httpd.shutdown()


def test_default_falls_back_to_an_indexed_folder(tmp_path):
    # Server started on a folder that was never indexed: /api/indexes must point
    # clients at an indexed folder instead (searching the raw default finds nothing).
    b = _make_indexed(tmp_path / "beta", {"car.txt": "the sedan has a strong engine"})
    unindexed = tmp_path / "plain"
    unindexed.mkdir()
    httpd, port = _start(unindexed)
    try:
        payload = _q(port, "/api/indexes")
        assert payload["default"] == str(b)
    finally:
        httpd.shutdown()


def test_manifest_stats_cached_by_index_run(tmp_path):
    # stats() must not re-COUNT a multi-GB manifest on every call: cached counts are
    # reused within one index generation and refreshed after the next reindex.
    from infogrep.manifest import Manifest

    target = _make_indexed(tmp_path / "docs", {"a.txt": "alpha", "b.txt": "beta"})
    cfg = Config.load(target)
    with Manifest(cfg.manifest_path) as m:
        assert m.stats()["n_files"] == 2
        assert json.loads(m.get_meta("stats_cache"))["n_files"] == 2
        # A poisoned cache proves the cached value is what's served...
        m.set_meta("stats_cache", m.get_meta("stats_cache").replace('"n_files": 2', '"n_files": 99'))
        assert m.stats()["n_files"] == 99
    (target / "c.txt").write_text("gamma")
    Indexer(Config.load(target)).reindex()
    with Manifest(cfg.manifest_path) as m:  # ...and a reindex refreshes it
        assert m.stats()["n_files"] == 3


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


def _post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode("utf-8")


def _fake_launchd(monkeypatch, agents_dir):
    import infogrep.scheduler as sched

    monkeypatch.setattr(sched, "LAUNCH_AGENTS", agents_dir)
    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(sched.sys, "platform", "darwin")


def test_schedule_toggle_api(tmp_path, monkeypatch):
    _fake_launchd(monkeypatch, tmp_path / "LaunchAgents")
    target = _make_indexed(tmp_path / "docs", {"a.txt": "alpha beta"})
    httpd, port = _start(target)
    try:
        q = urllib.parse.quote(str(target))
        # Turn daily reindex on -> agent plist written, /api/indexes reflects it.
        _, body = _post(port, f"/api/schedule?dir={q}&on=1")
        out = json.loads(body)
        assert out["ok"] is True and out["scheduled"] is True
        _, body = _get(port, "/api/indexes")
        entry = next(i for i in json.loads(body)["indexes"] if i["dir"] == str(target))
        assert entry["scheduled"] is True
        # Turn it off again -> agent removed.
        _, body = _post(port, f"/api/schedule?dir={q}&on=0")
        out = json.loads(body)
        assert out["ok"] is True and out["scheduled"] is False
    finally:
        httpd.shutdown()


def test_schedule_api_rejects_non_macos(tmp_path, monkeypatch):
    import infogrep.scheduler as sched

    monkeypatch.setattr(sched, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(sched.sys, "platform", "linux")
    target = _make_indexed(tmp_path / "docs", {"a.txt": "alpha"})
    httpd, port = _start(target)
    try:
        q = urllib.parse.quote(str(target))
        _, body = _post(port, f"/api/schedule?dir={q}&on=1")
        out = json.loads(body)
        assert out["ok"] is False
        assert "cron" in out["error"] or "systemd" in out["error"]
    finally:
        httpd.shutdown()


def test_forget_endpoint_removes_index(tmp_path, monkeypatch):
    _fake_launchd(monkeypatch, tmp_path / "LaunchAgents")
    target = _make_indexed(tmp_path / "docs", {"a.txt": "alpha"})
    cfg = Config.load(target)
    assert cfg.index_dir.is_dir()
    httpd, port = _start(target)
    try:
        q = urllib.parse.quote(str(target))
        _, body = _post(port, f"/api/forget?dir={q}")
        assert json.loads(body)["ok"] is True
        assert not cfg.index_dir.exists()  # side-car index gone
        assert (tmp_path / "docs" / "a.txt").exists()  # source folder untouched
        _, body = _get(port, "/api/indexes")
        payload = json.loads(body)
        assert payload["version"]  # UI shows the backend version
        assert all(i["dir"] != str(target) for i in payload["indexes"])
    finally:
        httpd.shutdown()


def test_full_rebuild_via_index_endpoint(tmp_path):
    target = _make_indexed(tmp_path / "docs", {"a.txt": "alpha"})
    httpd, port = _start(target)
    try:
        q = urllib.parse.quote(str(target))
        _, body = _post(port, f"/api/index?dir={q}&full=1")
        assert json.loads(body)["ok"] is True
        for _ in range(100):  # wait for the background rebuild to finish
            _, body = _get(port, f"/api/status?dir={q}")
            if not json.loads(body).get("indexing"):
                break
            time.sleep(0.1)
        info = json.loads(body)
        assert info["indexed"] is True and info["n_files"] == 1
    finally:
        httpd.shutdown()


def test_ping_says_which_indexes_this_server_reads(tmp_path, isolated_index_home):
    from infogrep import __version__

    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        # What an MCP server checks before handing its searches over.
        assert _q(port, "/api/ping") == {
            "version": __version__, "home": str(isolated_index_home.resolve()),
        }
    finally:
        httpd.shutdown()


_asked: list = []  # every _FakeAsk made, in order


class _FakeAsk:
    """Stands in for infogrep.ask.Ask: no dsh, an answer after two steps."""

    def __init__(self, question, directory=None, session=None, server=None, notes=False):
        import threading

        from infogrep.ask import AskError

        if question == "no dsh":
            raise AskError("DeepSeek Harness (dsh) is not installed")
        self.question, self.directory, self.session = question, directory, session
        self.server, self.notes = server, notes
        self.cancelled = False
        self._go = threading.Event()
        if question != "slow":
            self._go.set()
        _asked.append(self)

    def events(self):
        from infogrep.ask import AskError

        yield {"type": "session", "id": self.session or "session-new"}
        yield {"type": "step", "text": "Searching “berries”"}
        self._go.wait(10)
        if self.cancelled:
            raise AskError("cancelled")
        yield {"type": "step", "text": "Reading berry.txt"}
        yield {"type": "answer", "text": "Antioxidants.", "sources": ["/docs/berry.txt"]}

    def cancel(self):
        self.cancelled = True
        self._go.set()


def _answered(port, job_id, want):
    state = {}
    for _ in range(100):
        state = _q(port, f"/api/ask?id={job_id}")
        if state["status"] == want:
            break
        time.sleep(0.05)
    return state


def test_ask_api_runs_a_question_and_reports_steps_then_the_answer(tmp_path, monkeypatch):
    import infogrep.ask

    monkeypatch.setattr(infogrep.ask, "Ask", _FakeAsk)
    _asked.clear()
    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        assert "ask" in _q(port, "/api/indexes")  # whether dsh is there to answer with
        assert _q(port, "/api/ask") == {"status": "gone"}  # nothing asked yet

        q = urllib.parse.quote("What are berries rich in?")
        started = json.loads(_post(port, f"/api/ask?q={q}&session=session-7")[1])
        assert started == {"ok": True, "id": 1}
        state = _answered(port, 1, "done")
        assert state["steps"] == ["Searching “berries”", "Reading berry.txt"]
        assert state["answer"] == "Antioxidants." and state["sources"] == ["/docs/berry.txt"]
        assert state["session"] == "session-7" and state["question"] == "What are berries rich in?"

        run = _asked[0]
        assert run.session == "session-7"
        assert str(run.directory) == str(tmp_path.resolve())  # the server's default folder
        assert run.server == f"http://127.0.0.1:{port}"  # the agent's searches come back here
        assert run.notes is False  # the knowledge base stays out unless asked for

        # scope=all leaves the folder out, which means every indexed one
        _post(port, "/api/ask?q=again&scope=all&notes=1")
        assert _asked[1].directory is None and _asked[1].session is None
        assert _asked[1].notes is True
        assert _answered(port, 2, "done")["id"] == 2
        assert _q(port, "/api/ask?id=1") == {"status": "gone"}  # only the latest is kept
    finally:
        httpd.shutdown()


def test_ask_api_reports_what_stops_a_question(tmp_path, monkeypatch):
    import infogrep.ask

    monkeypatch.setattr(infogrep.ask, "Ask", _FakeAsk)
    _asked.clear()
    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        assert json.loads(_post(port, "/api/ask?q=")[1]) == {"ok": False, "error": "empty question"}
        refused = json.loads(_post(port, "/api/ask?q=" + urllib.parse.quote("no dsh"))[1])
        assert refused == {"ok": False, "error": "DeepSeek Harness (dsh) is not installed"}

        first = json.loads(_post(port, "/api/ask?q=slow")[1])["id"]
        assert _q(port, f"/api/ask?id={first}")["status"] == "running"
        assert json.loads(_post(port, "/api/ask/cancel")[1]) == {"ok": True}
        assert _answered(port, first, "cancelled")["error"] == "cancelled"

        # One question at a time: asking another stops the one in flight.
        _post(port, "/api/ask?q=slow")
        slow = _asked[-1]
        second = json.loads(_post(port, "/api/ask?q=quick")[1])["id"]
        assert slow.cancelled
        assert _answered(port, second, "done")["answer"] == "Antioxidants."
    finally:
        httpd.shutdown()


_filled: list = []  # every _FakeFill made, in order


class _FakeFill:
    """Stands in for the agent infogrep.fill.start sets to work: a step, then ``reply``."""

    reply = '{"fields": [{"id": "f1", "value": "Ada", "source": "/docs/cv.pdf"}], "note": "ok"}'

    def __init__(self, page, fields, note="", directory=None, session=None, server=None,
                 changed=False, memory=""):
        self.page, self.fields, self.note = page, fields, note
        self.directory, self.session, self.server = directory, session, server
        self.changed, self.memory = changed, memory
        self.cancelled = False
        _filled.append(self)

    def events(self):
        yield {"type": "session", "id": self.session or "session-form"}
        yield {"type": "step", "text": "Searching “name”"}
        yield {"type": "answer", "text": self.reply, "sources": []}

    def cancel(self):
        self.cancelled = True


def _post_form(port, path, form, origin=None):
    """POST a form's fields the way the extension does: as JSON, from its own origin."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="POST", data=json.dumps(form).encode(),
        headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


_FORM = {
    "page": {"title": "Sign up", "url": "https://example.org/join"},
    "fields": [{"id": "f1", "kind": "text", "label": "Name"},
               {"id": "f2", "kind": "email", "label": "Email", "value": "a@b.c"}],
    "note": "For me.",
}
_EXTENSION = "chrome-extension://abcdefghijklmnop"


def _filled_in(port, job_id, want):
    state = {}
    for _ in range(100):
        state = _q(port, f"/api/fill?id={job_id}")
        if state["status"] == want:
            break
        time.sleep(0.05)
    return state


def test_fill_api_has_the_agent_work_out_a_forms_values(tmp_path, monkeypatch):
    import infogrep.fill

    monkeypatch.setattr(infogrep.fill, "start", _FakeFill)
    _filled.clear()
    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        assert "fill" in _q(port, "/api/indexes")  # whether dsh is there to do it with
        assert _q(port, "/api/fill") == {"status": "gone"}  # nothing filled in yet

        started = _post_form(port, "/api/fill?scope=all", _FORM, origin=_EXTENSION)
        assert started == {"ok": True, "id": 1, "fields": 2}
        state = _filled_in(port, 1, "done")
        assert state["values"] == [{"id": "f1", "value": "Ada", "source": "/docs/cv.pdf"}]
        assert state["note"] == "ok" and state["steps"] == ["Searching “name”"]
        assert state["session"] == "session-form"

        run = _filled[0]
        assert run.page == _FORM["page"] and run.note == "For me."
        assert run.fields == infogrep.fill.clean_fields(_FORM["fields"])
        assert run.directory is None and run.session is None  # every folder, a new conversation
        assert run.server == f"http://127.0.0.1:{port}"

        assert run.changed is False

        # The next round of the same form, in the same conversation and the server's folder.
        _post_form(port, "/api/fill?session=session-form", {**_FORM, "changed": True})
        assert str(_filled[1].directory) == str(tmp_path.resolve())
        assert _filled[1].session == "session-form" and _filled[1].changed is True
        assert _filled_in(port, 2, "done")["id"] == 2
    finally:
        httpd.shutdown()


def test_fill_api_refuses_what_it_cannot_fill_and_reports_a_bad_reply(tmp_path, monkeypatch):
    import infogrep.fill

    monkeypatch.setattr(infogrep.fill, "start", _FakeFill)
    monkeypatch.setattr(_FakeFill, "reply", "I could not find anything.")
    _filled.clear()
    httpd, port = _start(_indexed_dir(tmp_path))
    try:
        assert _post_form(port, "/api/fill", {"fields": []}) == {
            "ok": False, "error": "no fields to fill in"}
        assert _post_form(port, "/api/fill", {}) == {"ok": False, "error": "no fields to fill in"}
        # A script on a web page can post here too; it gets nothing started.
        refused = _post_form(port, "/api/fill", _FORM, origin="https://evil.example")
        assert refused == {"ok": False, "error": "form filling is for the InfoGrep extension"}
        assert _filled == []

        job = _post_form(port, "/api/fill", _FORM, origin=_EXTENSION)["id"]
        state = _filled_in(port, job, "error")
        assert "did not reply with values, but with: I could not find anything." in state["error"]
        assert json.loads(_post(port, "/api/fill/cancel")[1]) == {"ok": True}
    finally:
        httpd.shutdown()


def test_fill_api_types_in_prose_only_when_its_file_has_it(tmp_path, monkeypatch):
    import infogrep.fill

    docs = _indexed_dir(tmp_path)  # berry.txt: "blueberries are rich in antioxidants..."
    reply = {"fields": [
        {"id": "f1", "value": "blueberries are rich in antioxidants",
         "source": str(docs / "berry.txt")},
        {"id": "f2", "value": "Blueberries cure everything.", "source": str(docs / "berry.txt")},
    ]}
    monkeypatch.setattr(infogrep.fill, "start", _FakeFill)
    monkeypatch.setattr(_FakeFill, "reply", json.dumps(reply))
    httpd, port = _start(docs)
    try:
        form = {"fields": [{"id": "f1", "kind": "richtext", "label": "About"},
                           {"id": "f2", "kind": "richtext", "label": "Claims"}]}
        job = _post_form(port, "/api/fill", form, origin=_EXTENSION)["id"]
        values = _filled_in(port, job, "done")["values"]
        assert [(v["id"], v["copied"]) for v in values] == [("f1", True), ("f2", False)]
    finally:
        httpd.shutdown()


def test_fill_api_remembers_what_the_agent_found_and_tells_the_next_form(tmp_path, monkeypatch):
    import infogrep.fill
    from infogrep.memory import FormMemory

    docs = _indexed_dir(tmp_path)
    reply = {"fields": [{"id": "f1", "value": "Ada", "source": str(docs / "berry.txt")},
                        {"id": "f2", "memory": "身份证号"}],
             "facts": [{"name": "姓名", "value": "Ada", "source": str(docs / "berry.txt"),
                        "stable": True},
                       {"name": "身份证号", "value": "110101", "source": str(docs / "car.txt"),
                        "sensitive": True}]}
    monkeypatch.setattr(infogrep.fill, "start", _FakeFill)
    monkeypatch.setattr(_FakeFill, "reply", json.dumps(reply))
    _filled.clear()
    httpd, port = _start(docs)
    try:
        assert _q(port, "/api/fill/memory")["facts"] == 0
        first = _post_form(port, "/api/fill", _FORM, origin=_EXTENSION)["id"]
        state = _filled_in(port, first, "done")
        assert state["learned"] == 2
        # f2 named a fact not remembered when the reply came: it is left out.
        assert [v["id"] for v in state["values"]] == ["f1"]
        assert "Nothing is remembered yet." in _filled[0].memory
        assert FormMemory.load().facts["身份证号"]["value"] == "110101"

        # The next form is told what is remembered, the hidden fact without its value,
        # and gets that value put in where the agent names it.
        second = _post_form(port, "/api/fill", _FORM, origin=_EXTENSION)["id"]
        state = _filled_in(port, second, "done")
        assert '{"name": "姓名", "value": "Ada"}' in _filled[1].memory
        assert '{"name": "身份证号", "hidden": true}' in _filled[1].memory
        assert "110101" not in _filled[1].memory
        assert state["values"][1] == {"id": "f2", "value": "110101", "remembered": "身份证号",
                                      "source": str(docs / "car.txt")}
        assert _q(port, "/api/fill/memory")["facts"] == 2

        # Off: nothing remembered is said, and nothing is learned.
        third = _post_form(port, "/api/fill", {**_FORM, "memory": False}, origin=_EXTENSION)["id"]
        assert _filled_in(port, third, "done")["learned"] == 0 and _filled[2].memory == ""
        # A later form of the same conversation already knows: it isn't told again.
        _post_form(port, "/api/fill?session=session-form", _FORM, origin=_EXTENSION)
        assert _filled[3].memory == ""
    finally:
        httpd.shutdown()


def test_a_notes_toggle_is_offered_only_where_a_vault_is_found(tmp_path, monkeypatch):
    from infogrep.retrieval import kb as kb_module

    monkeypatch.setattr(kb_module, "_registries", list)  # no Obsidian on this machine
    (tmp_path / "docs").mkdir()
    docs = _indexed_dir(tmp_path / "docs")
    cfg = Config.load(docs)
    (cfg.index_dir / "config.toml").write_text(_LIGHT_CONFIG + "[kb]\nenabled = true\n")
    httpd, port = _start(docs)
    try:
        assert [i["kb"] for i in _q(port, "/api/indexes")["indexes"]] == [False]
        vault = tmp_path / "vault"
        vault.mkdir()
        (cfg.index_dir / "config.toml").write_text(
            _LIGHT_CONFIG + f"[kb]\nenabled = true\nvault_path = '{vault}'\n")
        assert [i["kb"] for i in _q(port, "/api/indexes")["indexes"]] == [True]
    finally:
        httpd.shutdown()
