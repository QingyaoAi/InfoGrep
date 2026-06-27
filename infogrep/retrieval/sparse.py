"""Sparse retriever backed by Anserini/Lucene (BM25, optional RM3 PRF).

Full (re)build shells out to ``pyserini.index.lucene`` (the batch indexer) in a child
process. Incremental updates and search run in-process via jnius against Lucene/Anserini
Java classes directly — avoiding pyserini's Python wrapper, which imports torch (~580 MB).

Incremental updates open a Lucene ``IndexWriter`` on the existing index and reproduce
Anserini's exact field layout so updated docs are byte-compatible with batch-built ones:
  - ``id``        StringField (stored, exact term) — enables delete-by-term
  - ``contents``  indexed DOCS_AND_FREQS_AND_POSITIONS + term vectors, not stored
  - ``raw``       StoredField (the JSON record), not indexed
analyzed with Anserini's ``DefaultEnglishAnalyzer`` (Porter), the same as the batch path.
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

    # -- incremental update ------------------------------------------------

    def update(self, removed_ids, added_passages) -> int:
        """Apply a passage delta to the existing index via Lucene IndexWriter (no rebuild).

        Deletes ``removed_ids`` then upserts ``added_passages``. Order matters: all deletes
        run first so that re-added (modified) passages, appended afterwards, survive.
        """
        ensure_jdk()
        from jnius import cast
        from pyserini.pyclass import autoclass

        Analyzer = autoclass("io.anserini.analysis.DefaultEnglishAnalyzer")
        FSDirectory = autoclass("org.apache.lucene.store.FSDirectory")
        Paths = autoclass("java.nio.file.Paths")
        IndexWriter = autoclass("org.apache.lucene.index.IndexWriter")
        IndexWriterConfig = autoclass("org.apache.lucene.index.IndexWriterConfig")
        OpenMode = autoclass("org.apache.lucene.index.IndexWriterConfig$OpenMode")
        FieldType = autoclass("org.apache.lucene.document.FieldType")
        IndexOptions = autoclass("org.apache.lucene.index.IndexOptions")
        Document = autoclass("org.apache.lucene.document.Document")
        StringField = autoclass("org.apache.lucene.document.StringField")
        StoredField = autoclass("org.apache.lucene.document.StoredField")
        BinaryDocValuesField = autoclass("org.apache.lucene.document.BinaryDocValuesField")
        BytesRef = autoclass("org.apache.lucene.util.BytesRef")
        Field = autoclass("org.apache.lucene.document.Field")
        Store = autoclass("org.apache.lucene.document.Field$Store")
        Term = autoclass("org.apache.lucene.index.Term")
        TermQuery = autoclass("org.apache.lucene.search.TermQuery")
        JString = autoclass("java.lang.String")

        def delete(pid: str):
            # Delete-by-id. Use a TermQuery (not a bare Term): jnius can't disambiguate
            # IndexWriter.deleteDocuments(Term...) from (Query...), but a TermQuery is
            # unambiguously a Query.
            writer.deleteDocuments(TermQuery(Term("id", pid)))

        def charseq(s: str):
            # jnius can't pick Field's CharSequence ctor from a python str; wrap explicitly.
            return cast("java.lang.CharSequence", JString(s.encode("utf-8"), "UTF-8"))

        # Field layout matching Anserini's DefaultLuceneDocumentGenerator (see module docstring).
        contents_type = FieldType()
        contents_type.setIndexOptions(IndexOptions.DOCS_AND_FREQS_AND_POSITIONS)
        contents_type.setStoreTermVectors(True)  # needed for RM3 PRF
        contents_type.setTokenized(True)
        contents_type.setStored(False)
        contents_type.freeze()

        config = IndexWriterConfig(Analyzer.newDefaultInstance())
        config.setOpenMode(OpenMode.CREATE_OR_APPEND)
        writer = IndexWriter(FSDirectory.open(Paths.get(str(self.index_dir))), config)
        try:
            # Deletes first (removed + modified-old ids) so re-added passages, appended
            # afterwards, survive.
            for pid in removed_ids:
                delete(pid)
            n = 0
            for row in added_passages:
                pid = row["passage_id"]
                delete(pid)  # idempotent: dedupe against any existing doc with this id
                doc = Document()
                # Match Anserini's field layout exactly (id has BINARY doc values).
                doc.add(StringField("id", pid, Store.YES))
                doc.add(BinaryDocValuesField("id", BytesRef(charseq(pid))))
                doc.add(Field("contents", charseq(row["text"]), contents_type))
                doc.add(StoredField("raw", _row_to_json(row)))
                writer.addDocument(doc)
                n += 1
            writer.commit()
        finally:
            writer.close()
        self._searcher = None  # force reopen against the updated index
        return n

    # -- search ------------------------------------------------------------

    def _ensure_searcher(self):
        # Query Lucene via Anserini's SimpleSearcher directly (through jnius), NOT
        # pyserini's LuceneSearcher: the latter transitively imports torch +
        # transformers (~580 MB) for neural features we don't use. This path keeps the
        # process small (BM25 needs only the JVM).
        if self._searcher is None:
            ensure_jdk()
            from pyserini.pyclass import autoclass

            if not any(self.index_dir.glob("segments*")):
                raise FileNotFoundError(
                    f"No sparse index at {self.index_dir}. Run `infogrep index <dir>` first."
                )
            SimpleSearcher = autoclass("io.anserini.search.SimpleSearcher")
            self._searcher = SimpleSearcher(str(self.index_dir))
        return self._searcher

    def search(self, query: str, k: int = 10, prf: bool = False) -> list[Result]:
        searcher = self._ensure_searcher()
        # BM25 is the default similarity; only toggle RM3 PRF.
        if prf:
            searcher.set_rm3()
        else:
            searcher.unset_rm3()

        hits = searcher.search(query, k)
        results: list[Result] = []
        for hit in hits:
            raw = json.loads(hit.lucene_document.get("raw"))
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
