"""Question answering through DeepSeek Harness: what dsh is told, what is kept on disk,
and how a run is followed — against a stand-in ``dsh`` so no model is ever called."""

import os
import stat
import time

import pytest

from infogrep import ask, dsh

# A stand-in for `dsh`: records how it was called, then plays out one headless run.
FAKE_DSH = r'''#!/usr/bin/env python3
import json, os, sys, time

task = sys.stdin.read()
with open(os.environ["FAKE_DSH_RECORD"], "w") as fh:
    json.dump({"args": sys.argv[1:], "task": task, "cwd": os.getcwd(),
               "env": {k: os.environ.get(k) for k in
                       ("DSH_PERMISSION_MODE", "INFOGREP_DIR", "INFOGREP_SCOPE",
                        "INFOGREP_EXCLUDE", "INFOGREP_SERVER")}},
              fh)

def emit(**event):
    print(json.dumps(event), flush=True)

emit(type="session", sessionId="session-abc", cwd=os.getcwd())
mode = os.environ.get("FAKE_DSH_MODE", "ok")
if mode == "hang":
    time.sleep(60)
if mode == "fail":
    print("some MCP server log line", file=sys.stderr)
    print("dsh: llm/unauthorized: not signed in", file=sys.stderr)
    emit(type="final", text="")
    sys.exit(1)
emit(type="status", phase="turn_start", turn=1)
emit(type="tool_call", callId="1", tool="mcp__infogrep__search_hybrid", input={"query": "trip budget"})
emit(type="tool_result", callId="1", status="completed", result="{}")
emit(type="tool_call", callId="2", tool="mcp__infogrep__read_file", input={"path": "/docs/plan.docx"})
emit(type="text", text="thinking aloud")
emit(type="final", text="Transport takes 1,500 yuan.\n\nSources:\n" + os.environ["FAKE_DSH_SOURCE"])
'''


@pytest.fixture
def fake_dsh(tmp_path, monkeypatch):
    """Put the stand-in first on PATH; returns where it records its call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "dsh"
    exe.write_text(FAKE_DSH)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    source = tmp_path / "plan.docx"
    source.write_text("x")
    record = tmp_path / "dsh-call.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DSH_RECORD", str(record))
    monkeypatch.setenv("FAKE_DSH_SOURCE", str(source))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    return record


def _call(record):
    import json

    return json.loads(record.read_text())


# -- what dsh is told -------------------------------------------------------------


def test_patch_adds_the_agent_and_keeps_dsh_storage_under_infogrep_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    text = ask.patch_text()
    home = ask.ask_home()

    assert f"- id: {ask.ENTRY_ID}" in text
    assert "args: [ '-m', 'infogrep', 'mcp', '--read-only' ]" in text  # search tools only
    assert f"root: '{home / 'sessions'}'" in text
    assert f"root: '{home / 'storages'}'" in text
    assert "disabled" not in text


def test_patch_stands_in_for_a_server_already_registered_in_the_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setattr(dsh, "_dsh_rejects", lambda profile: None)
    profile = tmp_path / "dsh-home" / "profiles" / ask.PROFILE
    profile.mkdir(parents=True)
    dsh.register(ask.PROFILE, tmp_path, command="/opt/ig/bin/infogrep")

    text = ask.patch_text()
    # dsh refuses two servers with one name, so the profile's own is switched off for the run.
    assert f"- id: {dsh.ENTRY_ID}\n  name: '@deepseek-ai/dsh-mcp-client'\n  disabled: true\n" in text
    assert f"- id: {ask.ENTRY_ID}" in text


def test_patch_file_is_only_rewritten_when_it_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    path = ask.write_patch()
    assert path.read_text() == ask.patch_text()
    os.utime(path, (1, 1))
    assert ask.write_patch() == path and path.stat().st_mtime == 1  # untouched

    monkeypatch.setattr(ask.sys, "executable", "/elsewhere/python3")
    ask.write_patch()
    assert "command: '/elsewhere/python3'" in path.read_text()
    assert list(path.parent.glob("*.tmp")) == []


# -- conversations kept on disk ---------------------------------------------------


def _conversation(name, age_days):
    session = ask.ask_home() / "sessions" / "--workspace--" / name
    session.mkdir(parents=True)
    log = session / "session.v4.jsonl.zstd"
    log.write_text("log")
    cache = ask.ask_home() / "storages" / "session_projcache" / "sessions" / f"{name}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{}")
    when = time.time() - age_days * 86400
    for path in (log, cache):
        os.utime(path, (when, when))
    return session, cache


def test_prune_deletes_conversations_past_their_keep_time():
    old, old_cache = _conversation("session-old", ask.KEEP_DAYS + 1)
    recent, recent_cache = _conversation("session-recent", 1)

    assert ask.prune() == 1
    assert not old.exists() and not old_cache.exists()
    assert recent.is_dir() and recent_cache.is_file()
    assert ask.prune() == 0


def test_latest_session_is_the_one_written_to_last():
    assert ask.latest_session() is None
    _conversation("session-a", 3)
    _conversation("session-b", 1)
    _conversation("session-c", 2)
    assert ask.latest_session() == "session-b"


# -- reading an answer ------------------------------------------------------------


def test_sources_list_is_taken_off_the_answer(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b 笔记.md"
    a.write_text("x")
    b.write_text("x")

    text, files = ask.split_sources(f"It is 42.\n\nSources:\n- {a}\n- `{b}`\n")
    assert text == "It is 42."
    assert files == [str(a), str(b)]

    text, files = ask.split_sources(f"答案是 42。\n\n**来源：**\n1. {a}\n2. {a}")
    assert (text, files) == ("答案是 42。", [str(a)])


def test_sources_that_are_not_files_stay_in_the_answer(tmp_path):
    a = tmp_path / "a.pdf"
    a.write_text("x")
    answer = f"It is 42.\n\nSources:\n{a}\nProjects/Budget (a note in the vault)"

    text, files = ask.split_sources(answer)
    assert text == answer  # nothing the agent wrote is dropped
    assert files == [str(a)]
    assert ask.split_sources("No list here.") == ("No list here.", [])


def test_a_tool_call_reads_as_a_line_of_progress():
    describe = ask._describe
    assert describe({"tool": "mcp__infogrep__search_hybrid", "input": {"query": "预算"}}) == "Searching “预算”"
    assert describe({"tool": "mcp__infogrep__read_file", "input": {"path": "/d/plan.docx"}}) == "Reading plan.docx"
    assert describe({"tool": "read", "input": {"file_path": "/d/notes.md"}}) == "Reading notes.md"
    assert describe({"tool": "grep", "input": {"pattern": "x"}}) == "Using grep"


# -- one question -----------------------------------------------------------------


def test_a_question_runs_dsh_once_and_yields_steps_then_the_answer(tmp_path, fake_dsh):
    docs = tmp_path / "docs"
    docs.mkdir()
    run = ask.Ask("How much is transport?", directory=docs, server="http://127.0.0.1:7421")
    events = list(run.events())

    assert events[0] == {"type": "session", "id": "session-abc"}
    assert [e["text"] for e in events if e["type"] == "step"] == [
        "Searching “trip budget”", "Reading plan.docx",
    ]
    assert events[-1] == {"type": "answer", "text": "Transport takes 1,500 yuan.",
                          "sources": [str(tmp_path / "plan.docx")]}
    assert run.session == "session-abc"

    call = _call(fake_dsh)
    patch = str(ask.ask_home() / "dsh.patch.yml")
    assert call["args"] == ["--profile", "headless", "--patch", patch, "--json", "-"]
    assert "Question: How much is transport?" in call["task"]
    assert f"the folder {docs.resolve()}" in call["task"]
    assert os.path.samefile(call["cwd"], ask.ask_home() / "workspace")
    assert call["env"] == {  # read-only, and the scope the MCP server should search
        "DSH_PERMISSION_MODE": "read-only", "INFOGREP_DIR": str(docs.resolve()),
        "INFOGREP_SCOPE": "", "INFOGREP_SERVER": "http://127.0.0.1:7421",
        "INFOGREP_EXCLUDE": "kb",  # the knowledge base is slow: only searched on request
    }


def test_a_follow_up_continues_the_session_with_the_bare_question(fake_dsh, monkeypatch):
    monkeypatch.setenv("INFOGREP_DIR", "/left/over/from/the/environment")
    run = ask.Ask("And meals?", session="session-abc")  # no directory: every indexed folder
    assert list(run.events())[-1]["type"] == "answer"

    call = _call(fake_dsh)
    assert call["args"][-3:] == ["--session-id", "session-abc", "-"]
    assert call["task"] == "And meals?"  # the conversation already holds the instructions
    assert call["env"]["INFOGREP_SCOPE"] == "all" and call["env"]["INFOGREP_DIR"] is None


def test_the_knowledge_base_is_searched_only_when_asked_for(fake_dsh):
    list(ask.Ask("anything").events())
    assert _call(fake_dsh)["env"]["INFOGREP_EXCLUDE"] == "kb"

    list(ask.Ask("anything", notes=True).events())
    assert _call(fake_dsh)["env"]["INFOGREP_EXCLUDE"] == ""


def test_a_failed_run_reports_what_dsh_said(fake_dsh, monkeypatch):
    monkeypatch.setenv("FAKE_DSH_MODE", "fail")
    with pytest.raises(ask.AskError, match="llm/unauthorized: not signed in"):
        list(ask.Ask("anything").events())


def test_cancel_stops_the_run(fake_dsh, monkeypatch):
    monkeypatch.setenv("FAKE_DSH_MODE", "hang")
    run = ask.Ask("anything")
    events = run.events()
    assert next(events)["type"] == "session"
    started = time.time()
    run.cancel()
    with pytest.raises(ask.AskError, match="cancelled"):
        next(events)
    assert time.time() - started < 10 and run._proc.poll() is not None


def test_a_run_past_its_time_limit_is_stopped(fake_dsh, monkeypatch):
    monkeypatch.setenv("FAKE_DSH_MODE", "hang")
    with pytest.raises(ask.AskError, match="no answer after"):
        list(ask.Ask("anything", timeout=0.5).events())


def test_node_is_looked_for_beside_wherever_dsh_is_linked_from(tmp_path):
    # ~/.nvm/…/bin holds node and the real dsh; /usr/local/bin only has a link to it.
    nvm, local = tmp_path / "nvm" / "bin", tmp_path / "local" / "bin"
    nvm.mkdir(parents=True)
    local.mkdir(parents=True)
    (nvm / "dsh").write_text("#!/usr/bin/env node\n")
    (local / "dsh").symlink_to(nvm / "dsh")

    assert ask._link_dirs(str(local / "dsh")) == [str(local), str(nvm)]
    assert ask._link_dirs(str(nvm / "dsh")) == [str(nvm)]


def test_without_dsh_the_question_is_refused(monkeypatch):
    monkeypatch.setattr(ask, "dsh_command", lambda: None)
    with pytest.raises(ask.AskError, match="not installed"):
        ask.Ask("anything")
