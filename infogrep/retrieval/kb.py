"""Knowledge-base retriever backed by the Obsidian CLI (graph-aware).

Uses the official ``obsidian`` CLI against a live vault:
  - ``search``    seeds matches (ranked paths),
  - ``links`` / ``backlinks`` expand the graph by ``hops`` (both directions),
  - ``read``      fetches content for snippets.

Beyond plain matching, expanding along the link graph surfaces notes *connected* to a
hit even when they don't match the query — the value a knowledge base adds.

The CLI talks to the running Obsidian app and exits 0 even on failure, so we detect
errors by inspecting its output and raise ``FileNotFoundError`` (which the engine treats
as "skip this retriever") with a clear message when the app/vault is unavailable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import PurePosixPath

from ..config import Config
from .base import Result

_WORD_RE = re.compile(r"\w+")
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_SNIPPET_CHARS = 240
_HOP_DECAY = 0.4
_APP_DOWN_HINTS = ("make sure obsidian is running", "unable to find obsidian")

# The CLI answers in milliseconds, but now and then the app never answers a request at
# all: measured at about 1 call in 700 run back to back, and a few in 100 for searches
# that overlap (search-as-you-type). With ~30 calls per query that is a stalled query
# every few dozen searches, so a read that goes unanswered is given up on quickly and,
# reads being idempotent, asked once more. Don't serialize the calls with a lock to
# dodge the overlap: one lost call then stalls every query queued behind it.
# "Quickly" differs by call: a per-note lookup answers in milliseconds or never, but
# the vault search is slow on its own for common words ("a": 10 s on a ~900-note
# vault), and cutting that off would turn a slow answer into no answer.
_READ_TIMEOUTS = {"search": 15.0, "links": 2.0, "backlinks": 2.0, "read": 2.0}  # seconds
_WRITE_TIMEOUT = 30.0

# How long a note's links / backlinks / text are reused by later searches. Search-as-
# you-type runs a query per keystroke and successive prefixes land on mostly the same
# notes (about 3 in 4 per-note calls repeat while one query is typed); the price is that
# an edit made in Obsidian can take this long to show up in a snippet.
_LOOKUP_TTL = 30.0
_LOOKUP_CACHE_MAX = 2000


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _is_markdown(path: str) -> bool:
    return path.lower().endswith(".md")


class ObsidianCliError(FileNotFoundError):
    """Raised when the Obsidian CLI/app/vault is unavailable (engine skips kb)."""


class KnowledgeBaseIndex:
    """Graph-aware search over an Obsidian vault via the Obsidian CLI."""

    name = "kb"

    def __init__(self, config: Config, runner=None):
        self.config = config
        self.cli = config.kb.cli
        self.vault = config.kb.vault
        self.hops = max(0, config.kb.hops)
        self.search_limit = max(1, config.kb.search_limit)
        # Injectable for testing: runner(command, params) -> stdout string.
        self._run = runner or self._default_run
        self._cli_path: str | None = None  # self.cli resolved on PATH (see _spawn)
        self._lookups: dict[tuple[str, str], tuple[float, str]] = {}  # see _lookup

    # -- CLI plumbing ------------------------------------------------------

    def _default_run(self, command: str, params: dict[str, str]) -> str:
        if self._cli_path is None:
            self._cli_path = shutil.which(self.cli) or self.cli
        args = [self._cli_path, command]
        if self.vault:
            args.append(f"vault={self.vault}")
        # A None value is a bare flag (e.g. ``overwrite``), not a key=value pair.
        args += [k if v is None else f"{k}={v}" for k, v in params.items()]
        timeout = _READ_TIMEOUTS.get(command, _WRITE_TIMEOUT)
        try:
            try:
                proc = self._spawn(args, timeout)
            except subprocess.TimeoutExpired:
                if command not in _READ_TIMEOUTS:
                    raise  # a write may have landed; repeating it could duplicate the note
                proc = self._spawn(args, timeout)
        except FileNotFoundError as exc:
            raise ObsidianCliError(
                f"Obsidian CLI not found ('{self.cli}'). Install it or set kb.cli."
            ) from exc
        except subprocess.SubprocessError as exc:
            raise ObsidianCliError(f"Obsidian CLI failed: {exc}") from exc
        return (proc.stdout or "") + (proc.stderr or "")

    @staticmethod
    def _spawn(args: list[str], timeout: float) -> subprocess.CompletedProcess:
        # close_fds=False plus an absolute executable path lets subprocess use
        # posix_spawn instead of fork: forking a process that has the JVM mapped costs
        # ~12 ms per call, spawning ~4 ms. Python's own descriptors (the web server's
        # socket included) are non-inheritable; all the short-lived child additionally
        # sees is the JVM's read-only handles on its jar and module files.
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False, close_fds=False
        )

    def _call(self, command: str, params: dict[str, str]) -> str:
        out = self._run(command, params)
        low = out.strip().lower()
        if any(h in low for h in _APP_DOWN_HINTS):
            raise ObsidianCliError(
                "Obsidian is not running. Open the app (and the target vault) to use kb search."
            )
        return out

    def _lookup(self, command: str, path: str, **params: str) -> str:
        """A per-note CLI call whose answer is remembered for ``_LOOKUP_TTL`` seconds."""
        key = (command, path)
        now = time.monotonic()
        hit = self._lookups.get(key)
        if hit is not None and now - hit[0] < _LOOKUP_TTL:
            return hit[1]
        out = self._call(command, {"path": path, **params})  # a failure is never cached
        if len(self._lookups) >= _LOOKUP_CACHE_MAX:
            self._lookups.clear()
        self._lookups[key] = (now, out)
        return out

    @staticmethod
    def _parse_paths(out: str) -> list[str]:
        """Parse a CLI listing into vault paths, tolerant of json or line output."""
        text = out.strip()
        if not text or text.lower().startswith(("error:", "no ")):
            return []
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            items = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return [str(p) for p in items if _is_markdown(str(p))]

    # -- graph queries -----------------------------------------------------

    def _search(self, query: str) -> list[str]:
        out = self._call(
            "search",
            {"query": query, "limit": str(self.search_limit), "format": "json"},
        )
        return self._parse_paths(out)

    def _neighbors(self, path: str) -> set[str]:
        result: set[str] = set()
        for command in ("links", "backlinks"):
            try:
                out = self._lookup(command, path, format="json")
            except ObsidianCliError:
                continue  # one note's link lookup failing shouldn't abort the query
            result.update(self._parse_paths(out))
        return result

    def _read(self, path: str) -> str:
        try:
            out = self._lookup("read", path)
        except ObsidianCliError:
            return ""
        if out.strip().lower().startswith("error:"):
            return ""
        return _FRONTMATTER_RE.sub("", out, count=1)

    # -- note writing (used by kb_builder; confined to the agent folder) -----

    @staticmethod
    def _escape_content(text: str) -> str:
        """The CLI takes ``\\n``/``\\t`` escapes for newlines/tabs in content values."""
        return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")

    def read_note(self, path: str) -> str | None:
        """Raw note content (frontmatter included), or None when the note is missing."""
        out = self._call("read", {"path": path})
        if out.strip().lower().startswith("error:"):
            return None
        return out

    def create_note(self, path: str, content: str, overwrite: bool = False) -> None:
        """Create (or with ``overwrite`` replace) a note; parent folders are auto-created.

        Without ``overwrite`` the CLI silently writes to ``name 1.md`` when the path
        exists, so callers must check :meth:`read_note` first to stay idempotent.
        """
        params: dict[str, str | None] = {"path": path, "content": self._escape_content(content)}
        if overwrite:
            params["overwrite"] = None
        out = self._call("create", params)
        if out.strip().lower().startswith("error:"):
            raise ObsidianCliError(f"Obsidian create failed for {path}: {out.strip()}")

    def append_note(self, path: str, content: str) -> None:
        out = self._call("append", {"path": path, "content": self._escape_content(content)})
        if out.strip().lower().startswith("error:"):
            raise ObsidianCliError(f"Obsidian append failed for {path}: {out.strip()}")

    # -- expansion + snippets ---------------------------------------------

    def _expand(self, seeds: dict[str, float]) -> dict[str, float]:
        ranked = dict(seeds)
        frontier = dict(seeds)
        for _ in range(self.hops):
            nxt: dict[str, float] = {}
            for path, score in frontier.items():
                for neighbor in self._neighbors(path):
                    decayed = score * _HOP_DECAY
                    if decayed > ranked.get(neighbor, 0.0):
                        ranked[neighbor] = decayed
                        nxt[neighbor] = decayed
            if not nxt:
                break
            frontier = nxt
        return ranked

    def _snippet(self, text: str, query: str) -> str:
        low = text.lower()
        for term in _tokens(query):
            pos = low.find(term)
            if pos != -1:
                start = max(0, pos - 60)
                return " ".join(text[start : start + _SNIPPET_CHARS].split())
        return " ".join(text[:_SNIPPET_CHARS].split())

    # -- public ------------------------------------------------------------

    def search(self, query: str, k: int = 10, exts: tuple = ()) -> list[Result]:
        if exts and "md" not in exts:
            return []  # the vault only yields Markdown notes: don't bother the app
        paths = self._search(query)
        if not paths:
            return []
        # Seed scores from search rank (reciprocal rank); no scores from the CLI.
        seeds = {path: 1.0 / (rank + 1) for rank, path in enumerate(paths)}
        ranked = self._expand(seeds)

        ordered = sorted(ranked.items(), key=lambda kv: kv[1], reverse=True)[:k]
        results: list[Result] = []
        for path, score in ordered:
            snippet = self._snippet(self._read(path), query)
            results.append(
                Result(
                    doc_id=path,
                    passage_id=path,
                    path=path,
                    snippet=snippet or PurePosixPath(path).stem,
                    score=float(score),
                    retriever="kb",
                    page=None,
                    offset=None,
                )
            )
        return results
