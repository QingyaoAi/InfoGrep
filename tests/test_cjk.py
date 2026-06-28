"""CJK (Chinese) analyzer: bigram tokenization improves Chinese matching."""

import pytest

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.indexer import Indexer
from infogrep.retrieval.sparse import SparseIndex, make_analyzer


def _pyserini_available() -> bool:
    try:
        from infogrep.jvm import ensure_jdk

        ensure_jdk()
        import pyserini.search.lucene  # noqa: F401

        return True
    except Exception:
        return False


needs_sparse = pytest.mark.skipif(not _pyserini_available(), reason="pyserini/JDK21 not available")


@needs_sparse
def test_cjk_analyzer_is_bigram():
    from infogrep.jvm import ensure_jdk
    from pyserini.pyclass import autoclass

    ensure_jdk()
    AU = autoclass("io.anserini.analysis.AnalyzerUtils")
    zh = make_analyzer("zh")
    assert zh.getClass().getName().endswith("CJKAnalyzer")
    toks = AU.analyze(zh, "信息检索")
    assert [toks.get(i) for i in range(toks.size())] == ["信息", "息检", "检索"]


def _passage(pid, text, path):
    return {"passage_id": pid, "text": text, "path": path, "page": None, "offset": 0}


@needs_sparse
def test_cjk_search_matches_substring_phrase(tmp_path):
    # With the CJK (bigram) analyzer, a multi-char query matches docs sharing those
    # characters. Build two zh indexes and confirm the right doc ranks first.
    si = SparseIndex(tmp_path / "sparse", tmp_path / "cache", language="zh")
    si.build(iter([
        _passage("a#0", "信息检索系统的研究", "a.txt"),
        _passage("b#0", "自然语言处理方法", "b.txt"),
        _passage("c#0", "法律案件检索与多样性", "c.txt"),
    ]))
    assert si.built_language() == "zh"

    top = si.search("信息检索", k=3)
    assert top and top[0].path == "a.txt"
    # A different Chinese term routes to the right doc.
    assert si.search("自然语言", k=3)[0].path == "b.txt"
    assert si.search("法律案件", k=3)[0].path == "c.txt"


@needs_sparse
def test_changing_language_triggers_full_rebuild(tmp_path):
    import infogrep.retrieval.sparse as sparse_mod

    (tmp_path / "doc.txt").write_text("信息检索 retrieval system")
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = False
    cfg.sparse.language = "en"
    Indexer(cfg).reindex()
    assert SparseIndex(cfg.sparse_dir, cfg.cache_dir).built_language() == "en"

    # Switch to zh and reindex with no file changes -> must full-rebuild (re-tokenize).
    calls = {"build": 0, "update": 0}
    ob, ou = sparse_mod.SparseIndex.build, sparse_mod.SparseIndex.update
    import pytest as _pt  # noqa
    sparse_mod.SparseIndex.build = lambda self, *a, **k: (calls.__setitem__("build", calls["build"] + 1), ob(self, *a, **k))[1]
    sparse_mod.SparseIndex.update = lambda self, *a, **k: (calls.__setitem__("update", calls["update"] + 1), ou(self, *a, **k))[1]
    try:
        cfg.sparse.language = "zh"
        Indexer(cfg).reindex()
    finally:
        sparse_mod.SparseIndex.build, sparse_mod.SparseIndex.update = ob, ou

    assert calls["build"] == 1 and calls["update"] == 0
    assert SparseIndex(cfg.sparse_dir, cfg.cache_dir).built_language() == "zh"
    # Chinese substring query now works under the zh analyzer.
    assert SearchEngine(cfg).search_sparse("信息检索", k=3)
