"""Sparse retriever backed by Anserini/Lucene (BM25, optional RM3 PRF).

Both full build and incremental update run in-process via jnius against Lucene Java
classes directly (avoiding pyserini's Python wrapper, which imports torch ~580 MB). Going
through ``IndexWriter`` ourselves lets us use any analyzer — including the composite
English+CJK default that the batch ``pyserini.index.lucene`` tool can't express.

Documents are multi-field, reproducing Anserini's field layout:
  - ``id``                       StringField + BinaryDocValuesField (delete-by-term)
  - ``contents``/``filename``/``pathtext``  indexed with positions + term vectors, not stored
  - ``raw``                      StoredField (the JSON record), not indexed
See :func:`make_analyzer` for the analyzer per language. Search uses an in-process
``SimpleSearcher`` with the matching analyzer.
"""

from __future__ import annotations

import json
import re
import shutil
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

# How many times the original query terms are repeated relative to expansion terms,
# so PRF broadens recall without letting expansion override the user's query.
_PRF_ORIGINAL_WEIGHT = 2

# Common terms excluded from PRF expansion (English stopwords + generic file words).
_PRF_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were be been by at as it this that"
    " these those from but not we you he she they i my our your their its his her also"
    " which what when where who how can will would should could may might do does did has"
    " have had than then there here such into about over under via per etc page pages"
    " pdf doc docx ppt pptx xls xlsx txt file files figure table http https www com".split()
)

# Split paths/filenames on separators so "intro.tex" -> "intro tex" (the analyzer keeps
# "intro.tex" as one token otherwise, so "intro" wouldn't match).
_SEP_RE = re.compile(r"[/\\._\-]+")

_TOKEN_RE = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _filename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _tokenize_path(s: str) -> str:
    return _SEP_RE.sub(" ", s).strip()


def make_analyzer(language: str):
    """Lucene analyzer for a ``language`` code:

    - ``"en+zh"`` (default): a composite that does English Porter stemming AND CJK
      bigrams, so mixed English/Chinese (and Japanese/Korean) corpora work well.
      ``retrieving`` -> ``retriev`` and ``信息检索`` -> ``信息/息检/检索``.
    - ``"en"``: Anserini's DefaultEnglishAnalyzer (English only, Porter).
    - ``"zh"``/``"ja"``/``"ko"`` (single CJK): Anserini's CJKAnalyzer (bigrams).
    """
    from pyserini.pyclass import autoclass

    if "+" in language or language in ("multi", "cjk"):
        # standard tokenizer -> lowercase -> CJK bigrams (CJK only) -> Porter (Latin only)
        builder = autoclass("org.apache.lucene.analysis.custom.CustomAnalyzer").builder()
        builder.withTokenizer("standard")
        builder.addTokenFilter("lowercase")
        builder.addTokenFilter("cjkBigram")
        builder.addTokenFilter("porterStem")
        return builder.build()
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
        language: str = "en+zh",
        prf_fb_docs: int = 10,
        prf_fb_terms: int = 10,
    ):
        self.index_dir = index_dir
        self.cache_dir = cache_dir
        self.field_boosts = dict(field_boosts or DEFAULT_FIELD_BOOSTS)
        self.language = language
        self.prf_fb_docs = prf_fb_docs
        self.prf_fb_terms = prf_fb_terms
        self._searcher = None  # lazily constructed SimpleSearcher
        self._reader = None  # lazily opened IndexReader (for PRF term vectors)

    @property
    def _lang_marker(self) -> Path:
        return self.index_dir / "infogrep_lang.txt"

    def built_language(self) -> str:
        """The analyzer language the existing index was built with (default 'en')."""
        p = self._lang_marker
        return p.read_text().strip() if p.is_file() else "en"

    # -- build / incremental update ---------------------------------------
    #
    # Both go through a Lucene IndexWriter via jnius (torch-free, in-process), so any
    # analyzer can be used -- including the composite English+CJK default, which the
    # batch `pyserini.index.lucene` tool can't express (it only takes -language).
    # Documents reproduce Anserini's field schema exactly:
    #   id       StringField + BinaryDocValuesField (BINARY doc values)
    #   contents/filename/pathtext  DOCS_AND_FREQS_AND_POSITIONS + term vectors, not stored
    #   raw      StoredField

    def build(self, passages: Iterable) -> int:
        """(Re)build the whole index from a stream of manifest passage Rows."""
        if self.index_dir.exists():
            shutil.rmtree(self.index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        return self._write(passages, removed_ids=(), create=True)

    def update(self, removed_ids, added_passages) -> int:
        """Apply a passage delta to the existing index (no rebuild).

        Deletes ``removed_ids`` then upserts ``added_passages``; deletes run first so
        re-added (modified) passages, appended afterwards, survive.
        """
        return self._write(added_passages, removed_ids=removed_ids, create=False)

    def _write(self, passages: Iterable, removed_ids, create: bool) -> int:
        ensure_jdk()
        # Import pyserini.pyclass BEFORE jnius so it configures the full Anserini
        # classpath before the JVM starts (otherwise classes like CustomAnalyzer,
        # used by the en+zh analyzer, aren't found).
        from pyserini.pyclass import autoclass
        from jnius import cast

        # Build mode uses the configured language; incremental must match the existing index.
        language = self.language if create else self.built_language()
        analyzer = make_analyzer(language)

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
            # jnius can't disambiguate deleteDocuments(Term...) from (Query...);
            # a TermQuery is unambiguously a Query.
            writer.deleteDocuments(TermQuery(Term("id", pid)))

        def charseq(s: str):
            # jnius can't pick Field's CharSequence ctor from a python str; wrap explicitly.
            return cast("java.lang.CharSequence", JString(s.encode("utf-8"), "UTF-8"))

        contents_type = FieldType()
        contents_type.setIndexOptions(IndexOptions.DOCS_AND_FREQS_AND_POSITIONS)
        contents_type.setStoreTermVectors(True)  # needed for RM3 PRF
        contents_type.setTokenized(True)
        contents_type.setStored(False)
        contents_type.freeze()

        config = IndexWriterConfig(analyzer)
        config.setOpenMode(OpenMode.CREATE if create else OpenMode.CREATE_OR_APPEND)
        writer = IndexWriter(FSDirectory.open(Paths.get(str(self.index_dir))), config)
        try:
            for pid in removed_ids:
                delete(pid)
            n = 0
            for row in passages:
                pid = row["passage_id"]
                if not create:
                    delete(pid)  # dedupe against any existing doc with this id
                doc = Document()
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
        self._lang_marker.write_text(language)
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
            self._searcher.set_analyzer(make_analyzer(self.built_language()))
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

    def _index_reader(self):
        if self._reader is None:
            from pyserini.pyclass import autoclass

            FSDirectory = autoclass("org.apache.lucene.store.FSDirectory")
            Paths = autoclass("java.nio.file.Paths")
            DirectoryReader = autoclass("org.apache.lucene.index.DirectoryReader")
            self._reader = DirectoryReader.open(FSDirectory.open(Paths.get(str(self.index_dir))))
        return self._reader

    def _expansion_terms(self, searcher, query: str) -> list[str]:
        """Pseudo-relevance feedback terms from the *multi-field* top results.

        Anserini's RM3 builds feedback from a contents-only ranking, which for
        filename-matched queries pulls in the wrong (noisy) documents. Instead we expand
        from the documents the user actually sees (multi-field top hits), weighting terms
        by an RM1 estimate (per-doc relative frequency x doc score) times IDF so generic
        words don't dominate.
        """
        import math
        from collections import defaultdict

        from pyserini.pyclass import autoclass

        Term = autoclass("org.apache.lucene.index.Term")
        reader = self._index_reader()
        term_vectors = reader.termVectors()
        n_docs = max(1, reader.numDocs())

        qterms = set(_tokens(query))
        seeds = list(searcher.search_fields(query, self._fields_map(), self.prf_fb_docs))
        total = sum(h.score for h in seeds) or 1.0

        weights: dict[str, float] = defaultdict(float)
        for hit in seeds:
            terms = term_vectors.get(hit.lucene_docid, "contents")
            if terms is None:
                continue
            it = terms.iterator()
            freqs: dict[str, int] = {}
            doc_len = 0
            while True:
                br = it.next()
                if br is None:
                    break
                term = br.utf8ToString()
                tf = it.totalTermFreq()
                freqs[term] = tf
                doc_len += tf
            if doc_len == 0:
                continue
            w = hit.score / total
            for term, tf in freqs.items():
                weights[term] += w * (tf / doc_len)

        def keep(t: str) -> bool:
            return (
                t not in qterms
                and len(t) >= 2
                and any(ch.isalnum() for ch in t)
                and t not in _PRF_STOPWORDS
            )

        # Rank by RM1 weight, then re-rank the best by RM1 x IDF (discriminative terms).
        ranked = sorted((kv for kv in weights.items() if keep(kv[0])), key=lambda kv: -kv[1])
        scored = []
        for term, w in ranked[:50]:
            df = reader.docFreq(Term("contents", term)) or 1
            idf = math.log((n_docs + 1) / (df + 0.5))
            scored.append((term, w * idf))
        scored.sort(key=lambda kv: -kv[1])
        return [t for t, _ in scored[: self.prf_fb_terms]]

    def search(self, query: str, k: int = 10, prf: bool = False) -> list[Result]:
        searcher = self._ensure_searcher()

        effective_query = query
        if prf:
            try:
                expansion = self._expansion_terms(searcher, query)
            except Exception:
                expansion = []
            if expansion:
                # Emphasize the original query so expansion broadens recall without
                # overriding it; keeps strong filename/exact matches on top.
                effective_query = " ".join([query] * _PRF_ORIGINAL_WEIGHT + expansion)

        # Multi-field BM25 over contents + filename + path (boosted), so queries can
        # match the file name / path, not only the passage text.
        hits = searcher.search_fields(effective_query, self._fields_map(), k)
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
