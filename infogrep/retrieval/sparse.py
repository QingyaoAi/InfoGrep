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
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from ..jvm import ensure_jdk
from .base import Result

_SNIPPET_CHARS = 240

# Extra searchable fields (besides the default "contents") and their query-time boosts.
# Indexing the file name + path lets queries match on them, not just passage text.
# These hold *tokenized* values (the real path stays in the stored "raw" for citation).
META_FIELDS = ("filename", "pathtext")
DEFAULT_FIELD_BOOSTS = {"contents": 1.0, "filename": 2.0, "pathtext": 1.0}

# Split paths/filenames on separators so "intro.tex" -> "intro tex" (the analyzer keeps
# "intro.tex" as one token otherwise, so "intro" wouldn't match).
_SEP_RE = re.compile(r"[/\\._\-]+")


def _filename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _tokenize_path(s: str) -> str:
    return _SEP_RE.sub(" ", s).strip()


def make_analyzer(language: str):
    """Lucene analyzer for a language: DefaultEnglishAnalyzer for 'en', else the
    Anserini language-specific analyzer (CJKAnalyzer bigrams for zh/ja/ko)."""
    from pyserini.pyclass import autoclass

    if language and language != "en":
        return autoclass("io.anserini.analysis.AnalyzerMap").getLanguageSpecificAnalyzer(language)
    return autoclass("io.anserini.analysis.DefaultEnglishAnalyzer").newDefaultInstance()


def _row_to_json(row) -> str:
    """Serialize a manifest passage Row to a Pyserini JsonCollection line.

    ``filename``/``pathtext`` are tokenized for search; ``path`` keeps the real value
    (stored in ``raw``) so results cite the actual file.
    """
    path = row["path"]
    return json.dumps(
        {
            "id": row["passage_id"],
            "contents": row["text"],
            "filename": _tokenize_path(_filename(path)),
            "pathtext": _tokenize_path(path),
            "path": path,
            "page": row["page"],
            "offset": row["offset"],
        }
    )


class SparseIndex:
    """Build and query a Lucene/BM25 index over passages (multi-field)."""

    name = "sparse"

    def __init__(
        self,
        index_dir: Path,
        cache_dir: Path,
        field_boosts: dict | None = None,
        language: str = "en",
    ):
        self.index_dir = index_dir
        self.cache_dir = cache_dir
        self.field_boosts = dict(field_boosts or DEFAULT_FIELD_BOOSTS)
        self.language = language
        self._searcher = None  # lazily constructed SimpleSearcher

    @property
    def _lang_marker(self) -> Path:
        return self.index_dir / "infogrep_lang.txt"

    def built_language(self) -> str:
        """The analyzer language the existing index was built with (default 'en')."""
        p = self._lang_marker
        return p.read_text().strip() if p.is_file() else "en"

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
            "-language", self.language,
            "-storePositions", "-storeDocvectors", "-storeRaw",
            "-fields", *META_FIELDS,  # index filename + path as searchable fields
        ]
        # Capture Lucene's verbose INFO logging; only surface it if indexing fails.
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"pyserini indexing failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}"
            )
        self._lang_marker.write_text(self.language)  # record analyzer language
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

        # Use the analyzer the index was built with, so new docs tokenize consistently.
        analyzer = make_analyzer(self.built_language())
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

        config = IndexWriterConfig(analyzer)
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
                # Match Anserini's field layout exactly (id has BINARY doc values;
                # filename/path use the same FieldType as contents).
                doc.add(StringField("id", pid, Store.YES))
                doc.add(BinaryDocValuesField("id", BytesRef(charseq(pid))))
                doc.add(Field("contents", charseq(row["text"]), contents_type))
                doc.add(Field("filename", charseq(_tokenize_path(_filename(row["path"]))), contents_type))
                doc.add(Field("pathtext", charseq(_tokenize_path(row["path"])), contents_type))
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
            # Parse queries with the same analyzer the index was built with.
            lang = self.built_language()
            if lang != "en":
                self._searcher.set_language(lang)
        return self._searcher

    def _fields_map(self):
        """java.util.HashMap<String,Float> of field -> boost for multi-field search."""
        from pyserini.pyclass import autoclass

        HashMap = autoclass("java.util.HashMap")
        Float = autoclass("java.lang.Float")
        m = HashMap()
        for field, boost in self.field_boosts.items():
            m.put(field, Float(float(boost)))
        return m

    def search(self, query: str, k: int = 10, prf: bool = False) -> list[Result]:
        searcher = self._ensure_searcher()
        # BM25 is the default similarity; only toggle RM3 PRF.
        if prf:
            searcher.set_rm3()
        else:
            searcher.unset_rm3()

        # Multi-field BM25 over contents + filename + path (boosted), so queries can
        # match the file name / path, not only the passage text.
        hits = searcher.search_fields(query, self._fields_map(), k)
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
