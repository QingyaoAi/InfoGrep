import json

import pytest

from infogrep.config import Config
from infogrep.indexer import Indexer
from infogrep.retrieval.sparse import SparseIndex, _row_to_json


def test_row_to_json_shape():
    row = {
        "passage_id": "a.txt#0",
        "text": "hello world",
        "path": "a.txt",
        "page": 3,
        "offset": 0,
    }
    out = json.loads(_row_to_json(row))
    assert out == {
        "id": "a.txt#0",
        "contents": "hello world",
        "path": "a.txt",
        "page": 3,
        "offset": 0,
    }


def _pyserini_available() -> bool:
    try:
        from infogrep.jvm import ensure_jdk

        ensure_jdk()
        import pyserini.search.lucene  # noqa: F401

        return True
    except Exception:
        return False


pytestmark_integration = pytest.mark.skipif(
    not _pyserini_available(), reason="pyserini/JDK21 not available"
)


@pytestmark_integration
def test_index_and_search_end_to_end(tmp_path):
    (tmp_path / "fox.txt").write_text("The quick brown fox jumps over the lazy dog.")
    (tmp_path / "retrieval.md").write_text(
        "Dense and sparse retrieval can be fused with reciprocal rank fusion."
    )
    (tmp_path / "unrelated.txt").write_text("Bananas are a good source of potassium.")

    cfg = Config.load(tmp_path)
    Indexer(cfg).reindex()

    sparse = SparseIndex(cfg.sparse_dir, cfg.cache_dir)

    hits = sparse.search("fox", k=5)
    assert hits, "expected a hit for 'fox'"
    assert hits[0].path == "fox.txt"
    assert hits[0].retriever == "sparse"
    assert hits[0].score > 0

    hits = sparse.search("reciprocal rank fusion retrieval", k=5)
    assert hits[0].path == "retrieval.md"


@pytestmark_integration
def test_search_reflects_incremental_delete(tmp_path):
    (tmp_path / "a.txt").write_text("alpha unique-token-zebra content")
    (tmp_path / "b.txt").write_text("beta other content")
    cfg = Config.load(tmp_path)
    Indexer(cfg).reindex()

    sparse = SparseIndex(cfg.sparse_dir, cfg.cache_dir)
    assert sparse.search("unique-token-zebra", k=5)

    (tmp_path / "a.txt").unlink()
    Indexer(cfg).reindex()
    # Fresh searcher against the rebuilt index: the deleted doc is gone.
    assert SparseIndex(cfg.sparse_dir, cfg.cache_dir).search("unique-token-zebra", k=5) == []
