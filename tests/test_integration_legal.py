"""End-to-end integration test on a realistic, mixed-file 'paper' corpus.

Mirrors a real LaTeX paper directory (the kind of thing a user points InfoGrep at):
several .tex sources, a PDF, a .txt, plus non-content files that should be skipped.
Uses the default config (sparse on, dense off), then queries "legal".
"""

import pytest

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.indexer import Indexer


def _pyserini_available() -> bool:
    try:
        from infogrep.jvm import ensure_jdk

        ensure_jdk()
        import pyserini.search.lucene  # noqa: F401

        return True
    except Exception:
        return False


needs_sparse = pytest.mark.skipif(not _pyserini_available(), reason="pyserini/JDK21 not available")


def _legal_paper(root):
    (root / "1introduction.tex").write_text(
        r"\section{Introduction}"
        "\nLegal retrieval is important: searching among large-scale legal documents "
        "for a relevant case is common for lawyers and judges. Models such as BERT-PLI "
        "have been used in legal search to capture semantic relevance."
    )
    (root / "4dataset.tex").write_text(
        r"\section{Dataset Construction}"
        "\nTo explore the diverse intents of legal search users, we constructed a "
        "dataset with relevance labels over court case documents."
    )
    (root / "6experiment.tex").write_text(
        r"\section{Experiments}"
        "\nWe evaluate diversification metrics (alpha-nDCG) for legal case retrieval "
        "and compare against web search diversification baselines."
    )
    (root / "abstract.txt").write_text(
        "We study result diversification for legal case retrieval and propose an "
        "intent-aware model tailored to the legal domain."
    )
    # A real PDF (acmart-style doc stand-in).
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "ACM template. Formatting guide for the manuscript.")
    doc.save(str(root / "acmart.pdf"))
    doc.close()

    # Non-content files: no text extracted, but still indexed by name/path (stub).
    (root / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n notarealimage")
    (root / "refs.bib").write_text("@article{x, title={Legal IR}}")
    (root / "acmart.cls").write_text("% latex class file")


@needs_sparse
def test_index_and_query_legal_corpus(tmp_path):
    _legal_paper(tmp_path)
    cfg = Config.load(tmp_path)  # default: sparse on, dense off, kb off

    report = Indexer(cfg).reindex()
    # 5 with content (3 .tex + .txt + .pdf) + 3 name-only (.png/.bib/.cls) = 8 files.
    assert report.added == 8
    assert report.name_only == 3
    assert report.n_passages >= 8
    assert not report.errors

    engine = SearchEngine(cfg)

    # The headline query.
    hits = engine.search_sparse("legal", k=10)
    assert hits, "expected hits for 'legal'"
    paths = {h.path for h in hits}
    assert "1introduction.tex" in paths
    assert all(h.retriever == "sparse" for h in hits)

    # A phrase query should rank the most on-topic sources highly.
    top = engine.search_sparse("legal case retrieval diversification", k=5)
    assert top[0].path.endswith(".tex")
    assert top[0].score > 0

    # PRF (query expansion) still returns results.
    assert engine.search_sparse("diversification", k=5, prf=True)


@needs_sparse
def test_hybrid_defaults_to_sparse_only_when_dense_off(tmp_path):
    _legal_paper(tmp_path)
    cfg = Config.load(tmp_path)
    Indexer(cfg).reindex()

    out = SearchEngine(cfg).search_hybrid("legal case retrieval", k=5)
    # Dense is off by default -> hybrid uses sparse alone, no skips reported.
    assert out.used == ["sparse"]
    assert out.skipped == {}
    assert out.results and out.results[0].retriever == "hybrid"


@needs_sparse
def test_incremental_update_on_legal_corpus(tmp_path):
    _legal_paper(tmp_path)
    cfg = Config.load(tmp_path)
    Indexer(cfg).reindex()

    # Second run with no changes is a no-op.
    r2 = Indexer(cfg).reindex()
    assert (r2.added, r2.modified, r2.deleted) == (0, 0, 0)
    assert r2.unchanged == 8

    # Edit one source -> exactly one modified, and search reflects new content.
    (tmp_path / "7conclusion.tex").write_text(
        r"\section{Conclusion} Our legal diversification approach improves coverage."
    )
    r3 = Indexer(cfg).reindex()
    assert r3.added == 1
    assert any(h.path == "7conclusion.tex" for h in SearchEngine(cfg).search_sparse("coverage", k=5))
