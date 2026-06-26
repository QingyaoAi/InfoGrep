"""Sparse retriever backed by Pyserini (Lucene BM25, optional RM3 PRF).

The Lucene index is built from passages streamed out of the manifest (the single source
of truth from M1). Indexing shells out to ``pyserini.index.lucene`` so the heavy JVM work
runs in a child process; search uses an in-process ``LuceneSearcher``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from ..jvm import ensure_jdk
from .base import Result

_SNIPPET_CHARS = 240


def _row_to_json(row) -> str:
    """Serialize a manifest passage Row to a Pyserini JsonCollection line."""
    return json.dumps(
        {
            "id": row["passage_id"],
            "contents": row["text"],
            "path": row["path"],
            "page": row["page"],
            "offset": row["offset"],
        }
    )


class SparseIndex:
    """Build and query a Lucene/BM25 index over passages."""

    name = "sparse"

    def __init__(self, index_dir: Path, cache_dir: Path):
        self.index_dir = index_dir
        self.cache_dir = cache_dir
        self._searcher = None  # lazily constructed LuceneSearcher

    # -- build -------------------------------------------------------------

    def build(self, passages: Iterable) -> int:
        """(Re)build the index from a stream of manifest passage Rows. Returns doc count."""
        ensure_jdk()
        collection_dir = self.cache_dir / "sparse_collection"
        collection_dir.mkdir(parents=True, exist_ok=True)
        docs_file = collection_dir / "docs.jsonl"

        n = 0
        with docs_file.open("w", encoding="utf-8") as fh:
            for row in passages:
                fh.write(_row_to_json(row))
                fh.write("\n")
                n += 1

        # Rebuild cleanly: Lucene won't append into an existing populated index dir here.
        if self.index_dir.exists():
            shutil.rmtree(self.index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)

        # Anserini-style single-dash options.
        cmd = [
            sys.executable, "-m", "pyserini.index.lucene",
            "-collection", "JsonCollection",
            "-input", str(collection_dir),
            "-index", str(self.index_dir),
            "-generator", "DefaultLuceneDocumentGenerator",
            "-threads", "4",
            "-storePositions", "-storeDocvectors", "-storeRaw",
        ]
        # Capture Lucene's verbose INFO logging; only surface it if indexing fails.
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"pyserini indexing failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}"
            )
        self._searcher = None  # force reopen against the fresh index
        return n

    # -- search ------------------------------------------------------------

    def _ensure_searcher(self):
        if self._searcher is None:
            ensure_jdk()
            from pyserini.search.lucene import LuceneSearcher

            if not (self.index_dir / "segments_1").exists() and not any(
                self.index_dir.glob("segments*")
            ):
                raise FileNotFoundError(
                    f"No sparse index at {self.index_dir}. Run `infogrep index <dir>` first."
                )
            self._searcher = LuceneSearcher(str(self.index_dir))
        return self._searcher

    def search(self, query: str, k: int = 10, prf: bool = False) -> list[Result]:
        searcher = self._ensure_searcher()
        searcher.set_bm25()
        if prf:
            searcher.set_rm3()
        else:
            searcher.unset_rm3()

        hits = searcher.search(query, k=k)
        results: list[Result] = []
        for hit in hits:
            raw = json.loads(searcher.doc(hit.docid).raw())
            text = raw.get("contents", "")
            results.append(
                Result(
                    doc_id=raw.get("path", hit.docid),
                    passage_id=hit.docid,
                    path=raw.get("path", ""),
                    snippet=text[:_SNIPPET_CHARS],
                    score=float(hit.score),
                    retriever="sparse",
                    page=raw.get("page"),
                    offset=raw.get("offset"),
                )
            )
        return results
