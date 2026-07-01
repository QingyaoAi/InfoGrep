"""Folder/filename metadata-graph retriever.

Reads the ``graph.json`` produced by :func:`infogrep.ingest.graph.build_graph` (no
Obsidian app required — InfoGrep owns this graph) to find which folder(s) best match a
query by folder/file *name*, then expands to neighboring folders (parent, children,
siblings) so hybrid search can surface sibling files from the most relevant folder(s),
not only documents whose own content matched the query directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

from .base import Result

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_HOP_DECAY = 0.5
_NAME_WEIGHT = 2.0  # a query term matching the folder's own name is a stronger signal
_FILE_WEIGHT = 1.0  # ... than matching one of the files it happens to contain


def _tokenize(text: str) -> set[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) > 1}


class FolderGraphIndex:
    """Folder-metadata graph search: finds the most relevant folder(s) for a query and
    returns files that live in them (not just files whose own name/content matched)."""

    name = "graph"

    def __init__(self, index_dir: Path, hops: int = 1, max_folders: int = 5):
        self.graph_path = Path(index_dir) / "graph.json"
        self.hops = max(0, hops)
        self.max_folders = max(1, max_folders)

    def _load(self) -> dict:
        try:
            return json.loads(self.graph_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _score_folders(self, graph: dict, query_tokens: set[str]) -> dict[str, float]:
        if not query_tokens:
            return {}
        scores: dict[str, float] = {}
        for folder, node in graph.items():
            name_hits = query_tokens & set(node.get("name_tokens", ()))
            file_hits = query_tokens & set(node.get("file_tokens", ()))
            score = _NAME_WEIGHT * len(name_hits) + _FILE_WEIGHT * len(file_hits)
            if score > 0:
                scores[folder] = score
        return scores

    def _neighbors(self, graph: dict, folder: str) -> set[str]:
        node = graph.get(folder)
        if node is None:
            return set()
        neighbors = set(node.get("subfolders", ()))
        parent = node.get("parent")
        if parent is not None:
            neighbors.add(parent)
            sibling_node = graph.get(parent)
            if sibling_node:
                neighbors.update(sibling_node.get("subfolders", ()))
        neighbors.discard(folder)
        return neighbors

    def _collect_files(self, graph: dict, folder: str) -> list[str]:
        """Relative paths of every file directly in ``folder``, plus (recursively) in
        its subfolders — matching a container folder should surface its whole subtree,
        not just files listed on that exact note (many folders are pure organizers with
        no direct files of their own, e.g. a year-less "Taxes/" holding only year dirs).
        """
        node = graph.get(folder)
        if node is None:
            return []
        out = [f"{folder}/{name}" if folder else name for name in node.get("files", ())]
        for sub in node.get("subfolders", ()):
            out.extend(self._collect_files(graph, sub))
        return out

    def _expand(self, graph: dict, seeds: dict[str, float]) -> dict[str, float]:
        ranked = dict(seeds)
        frontier = dict(seeds)
        for _ in range(self.hops):
            nxt: dict[str, float] = {}
            for folder, score in frontier.items():
                for neighbor in self._neighbors(graph, folder):
                    decayed = score * _HOP_DECAY
                    if decayed > ranked.get(neighbor, 0.0):
                        ranked[neighbor] = decayed
                        nxt[neighbor] = decayed
            if not nxt:
                break
            frontier = nxt
        return ranked

    def search(self, query: str, k: int = 10) -> list[Result]:
        graph = self._load()
        if not graph:
            return []
        query_tokens = _tokenize(query)
        seeds = self._score_folders(graph, query_tokens)
        if not seeds:
            return []
        top_seeds = dict(
            sorted(seeds.items(), key=lambda kv: kv[1], reverse=True)[: self.max_folders]
        )
        ranked_folders = self._expand(graph, top_seeds)
        ordered_folders = sorted(
            ranked_folders.items(), key=lambda kv: kv[1], reverse=True
        )[: self.max_folders]

        results: list[Result] = []
        seen: set[str] = set()  # a file can be reached via more than one ranked folder
        for folder, score in ordered_folders:
            rels = self._collect_files(graph, folder)
            # Files whose own name also matches the query rank first within the folder.
            rels.sort(
                key=lambda rel: len(query_tokens & _tokenize(PurePosixPath(rel).name)),
                reverse=True,
            )
            for rel in rels:
                if rel in seen:
                    continue
                seen.add(rel)
                containing = str(PurePosixPath(rel).parent)
                containing = "" if containing == "." else containing
                results.append(
                    Result(
                        doc_id=rel,
                        passage_id=f"{rel}#0",
                        path=rel,
                        snippet=(
                            f'co-located in folder "{containing or "/"}" '
                            "(metadata graph match)"
                        ),
                        score=float(score),
                        retriever="graph",
                        page=None,
                        offset=None,
                    )
                )
                if len(results) >= k:
                    return results
        return results
