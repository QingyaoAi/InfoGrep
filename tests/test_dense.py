import numpy as np

from infogrep.config import Config
from infogrep.indexer import Indexer
from infogrep.retrieval.dense import DenseIndex
from infogrep.retrieval.embedders.cache import EmbeddingCache
from infogrep.retrieval.embedders.hashing import HashEmbedder


def _dense_cfg(tmp_path):
    cfg = Config.load(tmp_path)
    cfg.dense.embedder = "hash"
    cfg.sparse.enabled = False  # isolate dense
    return cfg


def test_hash_embedder_normalized_and_lexical():
    emb = HashEmbedder(dim=64)
    v = emb.embed(["hello world", "hello world"])
    assert v.shape == (2, 64)
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0)
    # identical text -> identical vectors
    assert np.allclose(v[0], v[1])


def test_embedding_cache_roundtrip(tmp_path):
    cache = EmbeddingCache(tmp_path / "emb.sqlite", model_id="hash")
    k = cache.key("alpha")
    assert cache.get_many([k]) == {}
    vec = np.arange(8, dtype=np.float32)
    cache.put_many([(k, vec)])
    got = cache.get_many([k])
    assert k in got and np.allclose(got[k], vec)
    cache.close()


def test_dense_build_and_semantic_ranking(tmp_path):
    (tmp_path / "fox.txt").write_text("The quick brown fox jumps over the lazy dog.")
    (tmp_path / "retrieval.md").write_text(
        "Dense and sparse retrieval fused with reciprocal rank fusion."
    )
    (tmp_path / "banana.txt").write_text("Bananas are a good source of potassium and fiber.")

    cfg = _dense_cfg(tmp_path)
    Indexer(cfg).reindex()

    di = DenseIndex(cfg)
    top = di.search("reciprocal rank fusion", k=3)
    assert top, "expected dense hits"
    assert top[0].path == "retrieval.md"
    assert top[0].retriever == "dense"
    # similarity score: higher is better, top should beat the rest
    assert top[0].score >= top[-1].score

    assert di.search("potassium fiber", k=1)[0].path == "banana.txt"


def test_dense_reflects_incremental_delete(tmp_path):
    (tmp_path / "a.txt").write_text("alpha unique zebra content here")
    (tmp_path / "b.txt").write_text("beta ordinary words here")
    cfg = _dense_cfg(tmp_path)
    Indexer(cfg).reindex()
    assert DenseIndex(cfg).search("alpha unique zebra", k=1)[0].path == "a.txt"

    (tmp_path / "a.txt").unlink()
    Indexer(cfg).reindex()
    paths = {h.path for h in DenseIndex(cfg).search("alpha unique zebra", k=5)}
    assert "a.txt" not in paths
