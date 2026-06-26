"""Knowledge-base retriever over an Obsidian vault (graph-aware).

Reads the vault format directly (Markdown notes + ``[[wikilinks]]``), so it works
headlessly with no running app. Beyond plain text matching, it expands the top
matches along the link graph by ``hops`` — surfacing notes that are *connected* to a
hit even if they don't match the query themselves. That graph context is the value a
knowledge base adds over flat sparse/dense search.

No separate index is built: the vault is read at query time, so results are always
current. (Vaults are typically small; notes are scored in a single streaming pass.)
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from .base import Result

_WORD_RE = re.compile(r"\w+")
# [[Target]], [[Target|alias]], [[Target#heading]], [[folder/Target]]
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_/-]+)")

_TITLE_BOOST = 2.0
_HOP_DECAY = 0.4  # score multiplier per graph hop
_SEED_POOL = 10  # how many text matches to seed graph expansion from
_SNIPPET_CHARS = 240


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


@dataclass
class Note:
    path: str  # relative posix path within the vault
    title: str  # file stem (display name)
    key: str  # lowercased stem (link target match key)
    text: str
    links: set[str] = field(default_factory=set)  # outgoing resolved target keys
    backlinks: set[str] = field(default_factory=set)  # notes that link to this one
    tags: set[str] = field(default_factory=set)

    def neighbors(self) -> set[str]:
        """Related notes in either direction (graph treated as undirected)."""
        return self.links | self.backlinks


def _link_target_key(raw: str) -> str:
    """Reduce a raw wikilink body to a note match key (strip alias/heading/folder)."""
    target = raw.split("|", 1)[0]  # drop alias
    target = target.split("#", 1)[0]  # drop heading anchor
    target = target.strip().rstrip("/")
    target = target.rsplit("/", 1)[-1]  # keep basename if a path was given
    return target.lower()


class KnowledgeBaseIndex:
    """Graph-aware search over an Obsidian vault."""

    name = "kb"

    def __init__(self, config: Config):
        self.config = config
        self.vault_path = (
            Path(config.kb.vault_path).expanduser() if config.kb.vault_path else None
        )
        self.hops = max(0, config.kb.hops)
        self._notes: dict[str, Note] | None = None

    # -- vault loading -----------------------------------------------------

    def _ensure_vault(self) -> Path:
        if self.vault_path is None or not self.vault_path.is_dir():
            raise FileNotFoundError(
                "knowledge-base vault not found. Set kb.vault_path (an Obsidian vault) "
                "and kb.enabled = true in the directory's .infogrep/config.toml."
            )
        return self.vault_path

    def _load(self) -> dict[str, Note]:
        if self._notes is not None:
            return self._notes
        root = self._ensure_vault()
        notes: dict[str, Note] = {}
        for md in root.rglob("*.md"):
            if ".infogrep" in md.parts or ".git" in md.parts:
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = md.relative_to(root).as_posix()
            note = Note(path=rel, title=md.stem, key=md.stem.lower(), text=text)
            note.links = {_link_target_key(m) for m in _WIKILINK_RE.findall(text)}
            note.tags = {t.lower() for t in _TAG_RE.findall(text)}
            notes[note.key] = note

        # Second pass: populate backlinks (reverse edges) for undirected expansion.
        for key, note in notes.items():
            for target in note.links:
                if target in notes:
                    notes[target].backlinks.add(key)
        self._notes = notes
        return notes

    # -- scoring + graph expansion ----------------------------------------

    def _text_scores(self, notes: dict[str, Note], query: str) -> dict[str, float]:
        qterms = set(_tokens(query))
        qtags = {t.lstrip("#").lower() for t in query.split() if t.startswith("#")}
        scores: dict[str, float] = {}
        for key, note in notes.items():
            ntoks = _tokens(note.text)
            if not ntoks:
                continue
            tf = Counter(ntoks)
            body = sum(tf[t] for t in qterms)
            title_hits = sum(1 for t in qterms if t in set(_tokens(note.title)))
            tag_hits = len(qtags & note.tags)
            raw = body + _TITLE_BOOST * title_hits + _TITLE_BOOST * tag_hits
            if raw > 0:
                scores[key] = raw / (1.0 + math.log(1 + len(ntoks)))
        return scores

    def _expand(self, notes: dict[str, Note], seeds: dict[str, float]) -> dict[str, float]:
        """BFS along outgoing links, decaying score per hop; keep the best score per note."""
        ranked = dict(seeds)
        frontier = dict(seeds)
        for _ in range(self.hops):
            nxt: dict[str, float] = {}
            for key, score in frontier.items():
                for target in notes[key].neighbors():
                    if target not in notes:  # dangling link
                        continue
                    decayed = score * _HOP_DECAY
                    if decayed > ranked.get(target, 0.0):
                        ranked[target] = decayed
                        nxt[target] = decayed
            if not nxt:
                break
            frontier = nxt
        return ranked

    def _snippet(self, note: Note, query: str) -> str:
        text = note.text
        low = text.lower()
        for term in _tokens(query):
            pos = low.find(term)
            if pos != -1:
                start = max(0, pos - 60)
                return text[start : start + _SNIPPET_CHARS].replace("\n", " ").strip()
        return text[:_SNIPPET_CHARS].replace("\n", " ").strip()

    # -- public ------------------------------------------------------------

    def search(self, query: str, k: int = 10) -> list[Result]:
        notes = self._load()
        text_scores = self._text_scores(notes, query)
        if not text_scores:
            return []
        seeds = dict(
            sorted(text_scores.items(), key=lambda kv: kv[1], reverse=True)[:_SEED_POOL]
        )
        ranked = self._expand(notes, seeds)

        ordered = sorted(ranked.items(), key=lambda kv: kv[1], reverse=True)[:k]
        results: list[Result] = []
        for key, score in ordered:
            note = notes[key]
            results.append(
                Result(
                    doc_id=note.path,
                    passage_id=note.path,
                    path=note.path,
                    snippet=self._snippet(note, query),
                    score=float(score),
                    retriever="kb",
                    page=None,
                    offset=None,
                )
            )
        return results
