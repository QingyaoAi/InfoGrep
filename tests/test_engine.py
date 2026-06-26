import pytest

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.indexer import Indexer


def _corpus(root):
    (root / "fox.txt").write_text("The quick brown fox jumps over the lazy dog.")
    (root / "retrieval.md").write_text(
        "Dense and sparse retrieval fused with reciprocal rank fusion."
    )
    (root / "banana.txt").write_text("Bananas are a good source of potassium and fiber.")


def _pyserini_available() -> bool:
    try:
        from infogrep.jvm import ensure_jdk

        ensure_jdk()
        import pyserini.search.lucene  # noqa: F401

        return True
    except Exception:
        return False


needs_sparse = pytest.mark.skipif(not _pyserini_available(), reason="pyserini/JDK21 not available")


def test_hybrid_default_runs_only_enabled(tmp_path):
    # Default retriever set = enabled ones only; disabled sparse simply isn't run.
    _corpus(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = True
    cfg.dense.embedder = "hash"
    cfg.sparse.enabled = False
    cfg.kb.enabled = False
    Indexer(cfg).reindex()

    out = SearchEngine(cfg).search_hybrid("reciprocal rank fusion", k=3)
    assert out.used == ["dense"]
    assert out.skipped == {}  # nothing explicitly requested, so nothing to skip
    assert out.results and out.results[0].path == "retrieval.md"
    assert out.results[0].retriever == "hybrid"


def test_hybrid_explicit_request_reports_skips(tmp_path):
    # Explicitly requesting disabled retrievers reports them as skipped with a reason.
    _corpus(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = True
    cfg.dense.embedder = "hash"
    cfg.sparse.enabled = False
    cfg.kb.enabled = False
    Indexer(cfg).reindex()

    out = SearchEngine(cfg).search_hybrid(
        "reciprocal rank fusion", k=3, retrievers=["sparse", "dense", "kb"]
    )
    assert out.used == ["dense"]
    assert "disabled" in out.skipped["sparse"]
    assert "disabled" in out.skipped["kb"]


@needs_sparse
def test_hybrid_fuses_sparse_and_dense(tmp_path):
    _corpus(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = True
    cfg.dense.embedder = "hash"  # keep it fast; fusion logic is what we test
    Indexer(cfg).reindex()

    out = SearchEngine(cfg).search_hybrid("reciprocal rank fusion retrieval", k=3)
    assert set(out.used) == {"sparse", "dense"}
    assert out.results
    # The doc both retrievers agree on should surface at the top.
    assert out.results[0].path == "retrieval.md"
    # No duplicate passages after fusion.
    keys = [(r.doc_id, r.passage_id) for r in out.results]
    assert len(keys) == len(set(keys))
