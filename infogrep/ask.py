"""Answer a question about the indexed files with a DeepSeek Harness (``dsh``) agent.

One question is one ``dsh headless`` process: it searches through InfoGrep's MCP tools,
reads the files it needs, prints an answer and exits. Nothing stays running between
questions, and everything it writes stays under ``$INFOGREP_HOME/ask``:

* The agent's setup reaches dsh as a patch overlay on the command line (``--patch``),
  so no dsh profile is edited and there is nothing to unregister afterwards.
* The overlay moves dsh's session log and caches to ``ask/sessions`` and
  ``ask/storages``. dsh never deletes a session, so :func:`prune` does, a week on.
* dsh runs under its read-only file policy with nobody there to approve an exception,
  so the agent can read the user's files and change none of them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from . import dsh
from .config import index_home, indexed_dirs

PROFILE = "headless"  # dsh's answer-one-task-and-exit profile
ENTRY_ID = "mcp-infogrep-ask"
KEEP_DAYS = 7  # how long a conversation can still be followed up on
TIMEOUT_S = 600  # a question still unanswered after this long is stopped

# Where dsh and node usually live. A server started by launchd or from the app bundle
# gets a bare PATH with neither on it.
_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin")

_PROMPT = """\
You are answering a question about the user's own files. InfoGrep has indexed them, and \
its tools are available to you as mcp__infogrep__*.

- Find evidence before you answer. Start with mcp__infogrep__search_hybrid, and search \
again with different wording when the first results don't settle it. The files may be in \
Chinese or in English whatever the language of the question: try both.
- A search result is only a short snippet. When the answer depends on details, read the \
file with mcp__infogrep__read_file (pass the result's abs_path); it reads PDF, Word, \
slides and spreadsheets as text.
- Searches cover {scope}. Those search tools and read_file are all this needs; leave \
index_status alone (it walks the whole folder and is slow).
- Answer only from what the files say. If they don't contain the answer, say so plainly \
instead of guessing.
- Reply in the language of the question. Keep the formatting light: short paragraphs or \
a simple list, no tables and no headings.
- This session is read-only: you cannot change, create or delete anything, so don't try.
- End with a line that says only "Sources:", followed by the absolute path of each file \
your answer relies on, one per line with nothing else on the line.

Question: {question}
"""

# The line that opens the closing list of files, and what may sit in front of an entry.
_SOURCES_HEADING = re.compile(
    r"^[\s>*#_-]*(sources?|来源|参考文件|参考来源)[\s*_]*[:：]?[\s*_]*$", re.IGNORECASE
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])?\s*")


class AskError(RuntimeError):
    """The question got no answer: dsh is missing, the run failed, or it was stopped."""


def ask_home() -> Path:
    """Everything question answering keeps on disk (override with ``INFOGREP_HOME``)."""
    return index_home() / "ask"


def dsh_command() -> str | None:
    """Path of the ``dsh`` executable, or None when DeepSeek Harness isn't installed."""
    return shutil.which("dsh", path=_search_path())


def _search_path() -> str:
    dirs = [os.environ.get("PATH", ""), *(os.path.expanduser(d) for d in _BIN_DIRS)]
    return os.pathsep.join(d for d in dirs if d)


def _link_dirs(path: str) -> list[str]:
    """The directory of an executable, and of each file it is a symlink to.

    ``dsh`` is a Node script (``#!/usr/bin/env node``), and node sits beside wherever
    npm put it. Under nvm, volta or fnm that is a directory of its own, so a ``dsh``
    linked into ``/usr/local/bin`` still has to find node back where it came from.
    """
    dirs = []
    for _ in range(16):  # a link to a link to...; never loop on a cycle
        dirs.append(os.path.dirname(path))
        if not os.path.islink(path):
            break
        path = os.path.join(os.path.dirname(path), os.readlink(path))
    return dirs


# -- what dsh is told -------------------------------------------------------------


def patch_text() -> str:
    """The overlay that turns dsh's headless profile into InfoGrep's answering agent."""
    home = ask_home()
    quote = dsh._quote
    text = (
        "# InfoGrep's question-answering agent: what `infogrep ask` lays over dsh's\n"
        f"# {PROFILE} profile. Rewritten when a question is asked; edits here are lost.\n"
    )
    if dsh.status(PROFILE) != "absent":
        # dsh loads one server per name, so stand in for the one the profile registers.
        text += (
            f"- id: {dsh.ENTRY_ID}\n"
            "  name: '@deepseek-ai/dsh-mcp-client'\n"
            "  disabled: true\n"
        )
    return text + (
        "- insert:\n"
        f"    - id: {ENTRY_ID}\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        f"        serverName: {dsh.SERVER_NAME}\n"
        "        transport: stdio\n"
        f"        command: {quote(sys.executable)}\n"
        "        args: [ '-m', 'infogrep', 'mcp', '--read-only' ]\n"
        "        # Reading a long scanned PDF runs past the default 60 s per tool call.\n"
        "        toolCallTimeoutMs: 180000\n"
        "# Keep this agent's sessions out of dsh's own home, where nothing deletes them.\n"
        "- id: session-persistence-jsonl\n"
        "  name: '@deepseek-ai/dsh-session-persistence-jsonl'\n"
        "  config:\n"
        f"    root: {quote(str(home / 'sessions'))}\n"
        "- id: storage-json\n"
        "  name: '@deepseek-ai/dsh-storage-json'\n"
        "  config:\n"
        f"    root: {quote(str(home / 'storages'))}\n"
    )


def write_patch() -> Path:
    """Put the overlay on disk (only rewritten when it would differ) and return its path."""
    path = ask_home() / "dsh.patch.yml"
    text = patch_text()
    if not path.is_file() or path.read_text() != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replaced in one step: another question may be starting dsh on it right now.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    return path


def _prompt(question: str, directory: Path | None, all_folders: bool) -> str:
    if all_folders:
        dirs = ", ".join(str(d) for d in indexed_dirs())
        scope = f"every indexed folder ({dirs})" if dirs else "every indexed folder"
    else:
        scope = f"the folder {directory}"
    return _PROMPT.format(scope=scope, question=question.strip())


# -- conversations kept on disk ---------------------------------------------------


def _sessions() -> list[Path]:
    """One directory per conversation: ``sessions/<working directory>/<session id>``."""
    return [p for p in (ask_home() / "sessions").glob("*/*") if p.is_dir()]


def _touched(session: Path) -> float:
    """When a conversation was last written to (its log grows; the directory doesn't change)."""
    try:
        return max((f.stat().st_mtime for f in session.iterdir()), default=session.stat().st_mtime)
    except OSError:
        return 0.0


def latest_session() -> str | None:
    """Id of the most recent conversation still on disk — the one a follow-up continues."""
    newest = max(_sessions(), key=_touched, default=None)
    return newest.name if newest else None


def prune(keep_days: float = KEEP_DAYS) -> int:
    """Delete conversations last touched more than ``keep_days`` ago; returns how many."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for session in _sessions():
        if _touched(session) < cutoff:
            shutil.rmtree(session, ignore_errors=True)
            removed += 1
    # dsh also keeps a small summary of each session beside its other caches.
    for cached in (ask_home() / "storages" / "session_projcache").rglob("*"):
        try:
            if cached.is_file() and cached.stat().st_mtime < cutoff:
                cached.unlink()
        except OSError:
            pass
    return removed


# -- one question ----------------------------------------------------------------


def split_sources(answer: str) -> tuple[str, list[str]]:
    """Take the closing "Sources:" list off an answer; returns the answer and the files.

    The list stays in the answer when some of its lines aren't files on disk (a note in
    the knowledge base, a remark), so that nothing the agent wrote is dropped.
    """
    lines = answer.strip().splitlines()
    start = next(
        (i for i in range(len(lines) - 1, -1, -1) if _SOURCES_HEADING.match(lines[i])), None
    )
    if start is None:
        return answer.strip(), []
    entries = [line for line in lines[start + 1:] if line.strip()]
    files: list[str] = []
    all_files = bool(entries)
    for line in entries:
        path = _BULLET.sub("", line, count=1).strip().strip("`*<>\"'")
        if os.path.isabs(path) and os.path.isfile(path):
            if path not in files:
                files.append(path)
        else:
            all_files = False
    if all_files:
        return "\n".join(lines[:start]).strip(), files
    return answer.strip(), files


def _describe(event: dict) -> str:
    """A tool call as one line of progress: what is being searched for, or read."""
    name = str(event.get("tool") or "").rsplit("__", 1)[-1]
    args = event.get("input") if isinstance(event.get("input"), dict) else {}
    if name.startswith("search_") and args.get("query"):
        return f"Searching “{args['query']}”"
    path = args.get("path") or args.get("file_path")
    if name.startswith("read") and path:
        return f"Reading {os.path.basename(str(path))}"
    return f"Using {name}" if name else "Working"


class Ask:
    """One question on its way to an answer: a running ``dsh headless`` process.

    Iterate :meth:`events` for its progress and the answer; :meth:`cancel` stops it from
    any thread. ``session`` continues an earlier conversation (see :func:`latest_session`),
    and ``server`` names the ``infogrep serve`` the agent's searches should go through.
    The Obsidian knowledge base is left out unless ``notes`` asks for it: it answers
    through the Obsidian app, far slower than the indexes, and an agent searches many times.
    """

    def __init__(
        self,
        question: str,
        directory: str | Path | None = None,
        all_folders: bool = False,
        session: str | None = None,
        server: str | None = None,
        notes: bool = False,
        timeout: float = TIMEOUT_S,
    ):
        exe = dsh_command()
        if exe is None:
            raise AskError(
                "DeepSeek Harness (dsh) is not installed, or not on PATH — "
                "see https://github.com/deepseek-ai/deepseek-harness"
            )
        if not question.strip():
            raise AskError("empty question")
        directory = Path(directory).expanduser().resolve() if directory else None
        if directory is None:
            all_folders = True
        prune()
        workspace = ask_home() / "workspace"  # an empty directory for the agent to start in
        workspace.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([*_link_dirs(exe), _search_path()])  # node, for dsh
        env["DSH_PERMISSION_MODE"] = "read-only"
        # dsh hands its environment on to the MCP server; that is how the scope gets there.
        env["INFOGREP_SCOPE"] = "all" if all_folders else ""
        env["INFOGREP_EXCLUDE"] = "" if notes else "kb"
        if directory:
            env["INFOGREP_DIR"] = str(directory)
        else:
            env.pop("INFOGREP_DIR", None)
        if server:
            env["INFOGREP_SERVER"] = server

        command = [exe, "--profile", PROFILE, "--patch", str(write_patch()), "--json"]
        if session:
            # The conversation already carries the instructions; send the question alone.
            command += ["--session-id", session]
            task = question.strip()
        else:
            task = _prompt(question, directory, all_folders)
        self.session = session
        self.cancelled = False
        self._timed_out = False
        self._timeout = timeout
        self._errors: deque[str] = deque(maxlen=200)  # the end of dsh's stderr, for a failure
        self._proc = subprocess.Popen(
            [*command, "-"],  # the task is read from stdin, however it is punctuated
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=workspace, env=env, text=True, encoding="utf-8",
            start_new_session=True,  # its own process group: dsh and the MCP server stop together
        )
        self._proc.stdin.write(task)
        self._proc.stdin.close()
        self._drain = threading.Thread(target=self._read_errors, daemon=True)
        self._drain.start()
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def events(self) -> Iterator[dict]:
        """Progress as it happens, ending with the answer.

        Yields ``{"type": "session", "id"}`` once, ``{"type": "step", "text"}`` for each
        search or file the agent turns to, and last ``{"type": "answer", "text",
        "sources"}``. Raises :class:`AskError` when the run fails, is cancelled or times
        out. Abandoning the iteration stops the run.
        """
        answer = failure = None
        try:
            for line in self._proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "session":
                    self.session = event.get("sessionId")
                    yield {"type": "session", "id": self.session}
                elif kind == "tool_call":
                    yield {"type": "step", "text": _describe(event)}
                elif kind == "error":
                    failure = event.get("message")
                elif kind == "final":
                    answer = event.get("text")
            code = self._proc.wait()
            self._drain.join(timeout=2)
            if self._timed_out:
                raise AskError(f"no answer after {self._timeout:.0f} seconds; stopped")
            if self.cancelled:
                raise AskError("cancelled")
            if code != 0 or not answer:
                raise AskError(failure or self._diagnostics() or f"dsh exited with status {code}")
        finally:
            self._timer.cancel()
            self._stop()
            self._proc.stdout.close()
        text, sources = split_sources(answer)
        yield {"type": "answer", "text": text, "sources": sources}

    def cancel(self) -> None:
        """Stop the run; a consumer of :meth:`events` then gets an :class:`AskError`."""
        self.cancelled = True
        self._stop()

    def _expire(self) -> None:
        self._timed_out = True
        self._stop()

    def _stop(self) -> None:
        if self._proc.poll() is not None:
            return
        self._signal(signal.SIGTERM)
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def _signal(self, sig: int) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(self._proc.pid, sig)
            else:
                self._proc.send_signal(sig)
        except OSError:
            pass  # already gone

    def _read_errors(self) -> None:
        with self._proc.stderr as stream:
            for line in stream:
                self._errors.append(line.rstrip("\n"))

    def _diagnostics(self) -> str:
        """Why dsh says it failed: its own ``dsh: …`` line, else the end of its stderr."""
        lines = list(self._errors)
        own = [line[len("dsh:"):].strip() for line in lines if line.startswith("dsh:")]
        return (own or [line for line in lines if line.strip()][-1:] or [""])[-1]
