import numpy as np
import pytest

from infogrep.config import Config
from infogrep.indexer import Indexer
from infogrep.retrieval.dense import DenseIndex
from infogrep.retrieval.embedders.cache import EmbeddingCache
from infogrep.retrieval.embedders.hashing import HashEmbedder


def _dense_cfg(tmp_path):
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = True  # dense is off by default now
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


def test_partial_build_is_not_treated_as_complete(tmp_path):
    # Simulate an aborted build (e.g. OOM): a Zvec dir exists but no completion marker.
    cfg = _dense_cfg(tmp_path)
    cfg.dense_dir.mkdir(parents=True)
    (cfg.dense_dir / "leftover.bin").write_bytes(b"partial")
    di = DenseIndex(cfg)
    assert di._exists() is False  # no embedder.json -> incomplete
    with pytest.raises(FileNotFoundError):
        di.search("anything", k=3)


def test_dense_incremental_update_path(tmp_path, monkeypatch):
    import infogrep.retrieval.dense as dense_mod

    (tmp_path / "a.txt").write_text("alpha unique-apple content")
    (tmp_path / "b.txt").write_text("beta unique-banana content")
    cfg = _dense_cfg(tmp_path)
    Indexer(cfg).reindex()  # full build

    calls = {"build": 0, "update": 0}
    orig_build, orig_update = dense_mod.DenseIndex.build, dense_mod.DenseIndex.update
    monkeypatch.setattr(
        dense_mod.DenseIndex, "build",
        lambda self, *a, **k: (calls.__setitem__("build", calls["build"] + 1), orig_build(self, *a, **k))[1],
    )
    monkeypatch.setattr(
        dense_mod.DenseIndex, "update",
        lambda self, *a, **k: (calls.__setitem__("update", calls["update"] + 1), orig_update(self, *a, **k))[1],
    )

    # Modify a, add c, delete b.
    (tmp_path / "a.txt").write_text("alpha unique-cherry content")
    (tmp_path / "c.txt").write_text("gamma unique-date content")
    (tmp_path / "b.txt").unlink()
    Indexer(cfg).reindex()

    # Second run took the incremental path, not a full rebuild.
    assert calls["update"] == 1
    assert calls["build"] == 0

    di = DenseIndex(cfg)
    assert di.search("unique-cherry", k=1)[0].path == "a.txt"  # modified content
    assert di.search("unique-date", k=1)[0].path == "c.txt"  # added
    assert "b.txt" not in {h.path for h in di.search("unique-banana", k=5)}  # deleted


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
