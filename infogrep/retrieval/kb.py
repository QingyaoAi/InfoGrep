"""Knowledge-base retriever over an Obsidian vault, read straight from its files.

A vault is a folder of Markdown notes, so this reads the notes itself: no Obsidian app,
no CLI, and nothing to keep running. A search

  - matches notes by BM25 over their text, with their title, aliases and tags weighted
    up (CJK as overlapping character pairs, the way the sparse index reads it), then
  - expands along the vault's links in both directions (``[[wikilinks]]``, embeds and
    Markdown links, resolved the way Obsidian resolves them), so notes *connected* to a
    match surface too even when they don't match the query — the value a knowledge base
    adds.

The notes are kept parsed in memory and re-read only when their file changes. A search
looks for changes at most every couple of seconds, so an edit made in Obsidian shows up
almost at once. The vault read is ``kb.vault_path``, else the vault called ``kb.vault`` in
Obsidian's own list of vaults, else the one most recently open in Obsidian.

``infogrep learn`` writes its notes through here as well, and only ever inside
``kb.agent_folder``.
"""

from __future__ import annotations

import bisect
import html
import json
import math
import os
import posixpath
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from ..config import Config, KnowledgeBaseConfig
from .base import Result

_SNIPPET_CHARS = 240
_HOP_DECAY = 0.4
_REFRESH_S = 2.0  # how often a search looks for notes changed on disk
_TITLE_WEIGHT = 3  # a word in a note's title, aliases or tags counts this many times
_K1, _B = 1.2, 0.75  # BM25
_PREFIX_TERMS = 20  # words an unfinished last word of the query stands for
_DATALESS = 0x40000000  # macOS: a file iCloud has moved off this Mac, until it is read

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"!?\[\[([^\]|#^]*)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_MDLINK_RE = re.compile(r"\]\(<?([^)>\n]+?)>?(?:\s+\"[^\"]*\")?\)")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
# Notes imported from Evernote and the like are full of HTML: its tags aren't text.
_HTML_RE = re.compile(r"<!--.*?-->|<(style|script)\b.*?</\1>|</?[A-Za-z][^<>]*>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"(?<![\w/#&])#([^\s#\[\]{}()\"',.;:!?。，、；：！？]+)")
_CJK = "぀-ヿ㐀-䶿一-鿿가-힯豈-﫿"
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_WORD_RE = re.compile(f"[^\\W{_CJK}_]+")  # a run of letters or digits that are not CJK
_SNIPPET_WORD_RE = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    """Words, lower-cased; CJK as overlapping character pairs (a lone character alone)."""
    low = text.lower()
    out = _WORD_RE.findall(low)
    for run in _CJK_RUN_RE.findall(low):
        out += [run[i : i + 2] for i in range(len(run) - 1)] or [run]
    return out


def _front_list(front: str, key: str) -> list[str]:
    """A list property of a note's frontmatter, in each form Obsidian writes one:
    ``key: a``, ``key: [a, b]``, or one ``- item`` per line below it."""
    m = re.search(rf"^{key}:[ \t]*(.*)$", front, re.MULTILINE)
    if not m:
        return []
    inline = m.group(1).strip()
    if inline.startswith("["):
        items = inline.strip("[]").split(",")
    elif inline:
        items = [inline]
    else:
        items = []
        for line in front[m.end() :].split("\n")[1:]:
            item = re.match(r"\s*-\s*(.*)", line)
            if not item:
                break
            items.append(item.group(1))
    return [s for s in (i.strip().strip("'\"").strip() for i in items) if s]


class VaultUnavailable(FileNotFoundError):
    """The vault can't be found or read; the engine then skips kb."""


# -- which vault ---------------------------------------------------------------------


def _registries() -> list[Path]:
    """Where Obsidian keeps its list of vaults on this platform."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library/Application Support/obsidian/obsidian.json"]
    if sys.platform.startswith("win"):
        appdata = Path(os.environ.get("APPDATA") or home / "AppData/Roaming")
        return [appdata / "obsidian/obsidian.json"]
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [config / "obsidian/obsidian.json",
            home / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json"]  # Flatpak


def vault_root(kb: KnowledgeBaseConfig) -> Path | None:
    """The folder of the vault ``kb`` means, or None when there is none to be found."""
    if kb.vault_path:
        return Path(kb.vault_path).expanduser()
    vaults: list[dict] = []
    for registry in _registries():
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        listed = data.get("vaults") if isinstance(data, dict) else None
        if isinstance(listed, dict):
            vaults += [v for v in listed.values() if isinstance(v, dict) and v.get("path")]
    if kb.vault:
        named = [v for v in vaults if Path(v["path"]).name == kb.vault]
        return Path(named[0]["path"]) if named else None
    # The vault open in Obsidian now, else the one open last: the one the CLI meant.
    vaults.sort(key=lambda v: (bool(v.get("open")), v.get("ts") or 0), reverse=True)
    return Path(vaults[0]["path"]) if vaults else None


def vault_roots() -> list[Path]:
    """The vaults that the indexed folders' knowledge bases read (where kb is enabled)."""
    from ..config import indexed_dirs

    roots: list[Path] = []
    for folder in indexed_dirs():
        kb = Config.load(folder).kb
        root = vault_root(kb) if kb.enabled else None
        if root is not None and root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def save_text(path: Path, text: str) -> None:
    """Write a note in one step, so that nothing (Obsidian, a sync) reads half of it. It
    keeps the permissions it had; a new note gets those of any other file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".infogrep-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


# -- the notes -------------------------------------------------------------------------


@dataclass
class _Note:
    path: str  # relative to the vault, "/"-separated
    stamp: tuple[int, int]  # (mtime_ns, size) when it was read
    body: str  # its text, without the frontmatter or any HTML tags
    names: list[str]  # what a link may call it: its file name and its aliases
    targets: list[str]  # where its links point, as written
    terms: Counter
    length: int


def _parse(full: str, rel: str, stamp: tuple[int, int]) -> _Note | None:
    try:
        text = Path(full).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    front, raw = (m.group(1), text[m.end() :]) if m else ("", text)
    body = html.unescape(_HTML_RE.sub(" ", raw)) if "<" in raw or "&" in raw else raw
    title = PurePosixPath(rel).stem
    aliases = _front_list(front, "aliases") + _front_list(front, "alias")
    prose = _CODE_RE.sub(" ", body)
    tags = _front_list(front, "tags") + _front_list(front, "tag") + _TAG_RE.findall(prose)
    # Obsidian counts a [[link]] in a property as a link, as much as one in the text.
    targets = _WIKILINK_RE.findall(front) + _WIKILINK_RE.findall(raw)
    for target in _MDLINK_RE.findall(raw):
        if "://" not in target and not target.startswith(("mailto:", "#")):
            targets.append(unquote(target.split("#", 1)[0]))
    terms = Counter(_tokens(body))
    for term in _tokens(" ".join([title, *aliases, *tags])):
        terms[term] += _TITLE_WEIGHT
    return _Note(rel, stamp, body, [title, *aliases], targets, terms, sum(terms.values()))


def _ignored(rel: str, filters: list[str]) -> bool:
    """Whether Obsidian's "Excluded files" setting leaves a note out of its search: each
    filter is a path the note's begins with, or a /regular expression/."""
    for f in filters:
        if len(f) > 2 and f.startswith("/") and f.endswith("/"):
            try:
                if re.search(f[1:-1], rel):
                    return True
            except re.error:
                continue
        elif rel.startswith(f):
            return True
    return False


class KnowledgeBaseIndex:
    """Graph-aware search over an Obsidian vault, from the vault's own files."""

    name = "kb"

    def __init__(self, config: Config, vault: str | os.PathLike | None = None):
        self.config = config
        self.hops = max(0, config.kb.hops)
        self.search_limit = max(1, config.kb.search_limit)
        self.agent_folder = config.kb.agent_folder.strip("/")
        self._vault = Path(vault) if vault else None
        self._notes: dict[str, _Note] = {}
        self._checked: float | None = None  # when the files were last looked at
        self._postings: dict[str, dict[str, int]] = {}
        self._vocabulary: list[str] = []
        self._average = 1.0
        self._links: dict[str, set[str]] = defaultdict(set)
        self._backlinks: dict[str, set[str]] = defaultdict(set)

    @property
    def root(self) -> Path:
        """The vault's folder; raises :class:`VaultUnavailable` when there is none."""
        if self._vault is None:
            self._vault = vault_root(self.config.kb)
        if self._vault is None or not self._vault.is_dir():
            where = f" ({self._vault})" if self._vault else ""
            self._vault = None  # look again next time: Obsidian may have it by then
            raise VaultUnavailable(
                f"no Obsidian vault found{where}: set [kb] vault_path (or vault) in the config"
            )
        return self._vault

    # -- reading the vault ---------------------------------------------------

    def _filters(self, root: Path) -> list[str]:
        try:
            data = json.loads((root / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        filters = data.get("userIgnoreFilters") if isinstance(data, dict) else None
        return [f for f in filters or [] if isinstance(f, str) and f]

    def _refresh(self, force: bool = False) -> None:
        """Re-read the notes whose files changed since the last look, which is at most
        every ``_REFRESH_S`` seconds unless ``force``."""
        now = time.monotonic()
        if not force and self._checked is not None and now - self._checked < _REFRESH_S:
            return
        root = self.root
        filters = self._filters(root)
        seen: set[str] = set()
        changed = self._checked is None
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]  # .obsidian, .trash
            for name in files:
                if not name.lower().endswith(".md"):
                    continue
                full = os.path.join(folder, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                # A note iCloud moved off this Mac would block the search on a download.
                if getattr(st, "st_flags", 0) & _DATALESS or _ignored(rel, filters):
                    continue
                seen.add(rel)
                stamp = (st.st_mtime_ns, st.st_size)
                note = self._notes.get(rel)
                if note is None or note.stamp != stamp:
                    parsed = _parse(full, rel, stamp)
                    if parsed is not None:
                        self._notes[rel] = parsed
                        changed = True
        for gone in set(self._notes) - seen:
            del self._notes[gone]
            changed = True
        self._checked = time.monotonic()
        if changed:
            self._build()

    def _build(self) -> None:
        """Derive what searches use from the notes: the word index and the link graph."""
        postings: dict[str, dict[str, int]] = defaultdict(dict)
        for note in self._notes.values():
            for term, n in note.terms.items():
                postings[term][note.path] = n
        self._postings = dict(postings)
        self._vocabulary = sorted(postings)
        self._average = sum(n.length for n in self._notes.values()) / max(1, len(self._notes))
        by_path = {note.path[:-3].lower(): note.path for note in self._notes.values()}
        by_name: dict[str, list[str]] = defaultdict(list)
        for note in self._notes.values():
            for name in note.names:
                by_name[name.lower()].append(note.path)
        self._links, self._backlinks = defaultdict(set), defaultdict(set)
        for note in self._notes.values():
            for target in note.targets:
                found = self._resolve(note.path, target, by_path, by_name)
                if found and found != note.path:
                    self._links[note.path].add(found)
                    self._backlinks[found].add(note.path)

    @staticmethod
    def _resolve(source: str, target: str, by_path: dict, by_name: dict) -> str | None:
        """The note a link points at, as Obsidian picks it: a path from the linking note's
        folder or from the vault's root, else the note with that name (or alias), the one
        nearest the linking note when several share it. None for an attachment, or for a
        note that doesn't exist yet."""
        key = target.strip().replace("\\", "/").lower()
        key = key.removesuffix(".md").strip("/")
        if not key:
            return None
        folder = posixpath.dirname(source).lower()
        for path in (posixpath.normpath(posixpath.join(folder, key)) if folder else key, key):
            if path in by_path:
                return by_path[path]
        named = by_name.get(posixpath.basename(key), [])
        if "/" in key:  # a partial path ("sub/note" for "a/sub/note.md")
            named = [p for p in named if p.lower().removesuffix(".md").endswith("/" + key)]
        if not named:
            return None
        return min(named, key=lambda p: (posixpath.dirname(p).lower() != folder, len(p), p))

    # -- searching -------------------------------------------------------------

    def _score(self, query: str) -> Counter:
        terms = set(_tokens(query))
        # A last word still being typed ("alp") stands for the words it begins ("alpha").
        words = _WORD_RE.findall(query.lower())
        if words and query == query.rstrip() and words[-1] not in self._postings:
            last = words[-1]
            start = bisect.bisect_left(self._vocabulary, last)
            for term in self._vocabulary[start : start + _PREFIX_TERMS]:
                if not term.startswith(last):
                    break
                terms.add(term)
        n = len(self._notes)
        scores: Counter = Counter()
        for term in terms:
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = math.log(1 + (n - len(posting) + 0.5) / (len(posting) + 0.5))
            for path, tf in posting.items():
                norm = _K1 * (1 - _B + _B * self._notes[path].length / self._average)
                scores[path] += idf * tf * (_K1 + 1) / (tf + norm)
        return scores

    def _expand(self, seeds: dict[str, float]) -> dict[str, float]:
        ranked = dict(seeds)
        frontier = dict(seeds)
        for _ in range(self.hops):
            nxt: dict[str, float] = {}
            for path, score in frontier.items():
                for neighbor in self._links.get(path, set()) | self._backlinks.get(path, set()):
                    decayed = score * _HOP_DECAY
                    if decayed > ranked.get(neighbor, 0.0):
                        ranked[neighbor] = decayed
                        nxt[neighbor] = decayed
            if not nxt:
                break
            frontier = nxt
        return ranked

    @staticmethod
    def _snippet(text: str, query: str) -> str:
        low = text.lower()
        for term in _SNIPPET_WORD_RE.findall(query.lower()) + _tokens(query):
            pos = low.find(term)
            if pos != -1:
                start = max(0, pos - 60)
                return " ".join(text[start : start + _SNIPPET_CHARS].split())
        return " ".join(text[:_SNIPPET_CHARS].split())

    def search(self, query: str, k: int = 10, exts: tuple = ()) -> list[Result]:
        if exts and "md" not in exts:
            return []  # the vault only yields Markdown notes
        self._refresh()
        best = sorted(self._score(query).items(), key=lambda kv: (-kv[1], kv[0]))
        # Seeds score by rank, as before: fused search goes by ranks anyway.
        seeds = {path: 1.0 / (rank + 1) for rank, (path, _) in enumerate(best[: self.search_limit])}
        ranked = sorted(self._expand(seeds).items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [
            Result(
                doc_id=path,
                passage_id=path,
                path=path,
                snippet=self._snippet(self._notes[path].body, query) or PurePosixPath(path).stem,
                score=float(score),
                retriever="kb",
                page=None,
                offset=None,
            )
            for path, score in ranked
        ]

    # -- note writing (kb_builder; confined to the agent folder) -------------

    def _file(self, path: str, writing: bool = False) -> Path:
        rel = PurePosixPath(path)
        if rel.is_absolute() or ".." in rel.parts or rel.suffix.lower() != ".md":
            raise ValueError(f"not a note in the vault: {path}")
        if writing and rel.parts[: len(PurePosixPath(self.agent_folder).parts)] != \
                PurePosixPath(self.agent_folder).parts:
            raise ValueError(f"InfoGrep only writes notes inside {self.agent_folder}/: {path}")
        return self.root / rel

    def read_note(self, path: str) -> str | None:
        """Raw note content (frontmatter included), or None when the note is missing."""
        try:
            return self._file(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def create_note(self, path: str, content: str, overwrite: bool = False) -> None:
        """Create (or with ``overwrite`` replace) a note; parent folders are created."""
        file = self._file(path, writing=True)
        if file.exists() and not overwrite:
            raise FileExistsError(f"note already exists: {path}")
        save_text(file, content)

    def append_note(self, path: str, content: str) -> None:
        file = self._file(path, writing=True)
        text = file.read_text(encoding="utf-8") if file.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        save_text(file, text + content + ("" if content.endswith("\n") else "\n"))
