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
import subprocess
from pathlib import PurePosixPath

from ..config import Config
from .base import Result

_WORD_RE = re.compile(r"\w+")
_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_SNIPPET_CHARS = 240
_HOP_DECAY = 0.4
_APP_DOWN_HINTS = ("make sure obsidian is running", "unable to find obsidian")


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

    # -- CLI plumbing ------------------------------------------------------

    def _default_run(self, command: str, params: dict[str, str]) -> str:
        args = [self.cli, command]
        if self.vault:
            args.append(f"vault={self.vault}")
        args += [f"{k}={v}" for k, v in params.items()]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except FileNotFoundError as exc:
            raise ObsidianCliError(
                f"Obsidian CLI not found ('{self.cli}'). Install it or set kb.cli."
            ) from exc
        except subprocess.SubprocessError as exc:
            raise ObsidianCliError(f"Obsidian CLI failed: {exc}") from exc
        return (proc.stdout or "") + (proc.stderr or "")

    def _call(self, command: str, params: dict[str, str]) -> str:
        out = self._run(command, params)
        low = out.strip().lower()
        if any(h in low for h in _APP_DOWN_HINTS):
            raise ObsidianCliError(
                "Obsidian is not running. Open the app (and the target vault) to use kb search."
            )
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
                out = self._call(command, {"path": path, "format": "json"})
            except ObsidianCliError:
                continue  # one note's link lookup failing shouldn't abort the query
            result.update(self._parse_paths(out))
        return result

    def _read(self, path: str) -> str:
        try:
            out = self._call("read", {"path": path})
        except ObsidianCliError:
            return ""
        if out.strip().lower().startswith("error:"):
            return ""
        return _FRONTMATTER_RE.sub("", out, count=1)

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

    def search(self, query: str, k: int = 10) -> list[Result]:
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
