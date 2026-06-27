import json

import pytest

from infogrep.config import Config
from infogrep.indexer import Indexer
from infogrep.retrieval.sparse import SparseIndex, _row_to_json


def test_row_to_json_shape():
    row = {
        "passage_id": "docs/a.txt#0",
        "text": "hello world",
        "path": "docs/a.txt",
        "page": 3,
        "offset": 0,
    }
    out = json.loads(_row_to_json(row))
    assert out == {
        "id": "docs/a.txt#0",
        "contents": "hello world",
        "filename": "a txt",  # tokenized basename (searchable)
        "pathtext": "docs a txt",  # tokenized path (searchable)
        "path": "docs/a.txt",  # real path (stored for citation)
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


def _passage(pid, text, path):
    return {"passage_id": pid, "text": text, "path": path, "page": None, "offset": 0}


@pytestmark_integration
def test_multifield_matches_filename_and_path(tmp_path):
    from infogrep.retrieval.sparse import SparseIndex

    si = SparseIndex(tmp_path / "sparse", tmp_path / "cache")
    si.build(iter([
        # The word "introduction" is in the FILENAME, not the passage text.
        _passage("introduction.tex#0", "we present a new ranking model", "papers/introduction.tex"),
        # "neural" only appears in the PATH directory.
        _passage("m.tex#0", "experimental results and analysis", "neural/m.tex"),
        _passage("other.txt#0", "completely unrelated grocery list", "misc/other.txt"),
    ]))

    # Match by filename token.
    assert si.search("introduction", k=5)[0].path == "papers/introduction.tex"
    # Match by path component.
    assert si.search("neural", k=5)[0].path == "neural/m.tex"
    # Content still matches.
    assert si.search("ranking model", k=5)[0].path == "papers/introduction.tex"


@pytestmark_integration
def test_multifield_works_after_incremental_add(tmp_path):
    from infogrep.retrieval.sparse import SparseIndex

    si = SparseIndex(tmp_path / "sparse", tmp_path / "cache")
    si.build(iter([_passage("a.txt#0", "first document body", "a.txt")]))
    si.update(removed_ids=set(),
              added_passages=iter([_passage("budget_report.pdf#0", "quarterly figures", "finance/budget_report.pdf")]))
    # Incrementally-added doc is findable by its filename token.
    assert si.search("budget", k=5)[0].path == "finance/budget_report.pdf"


@pytestmark_integration
def test_incremental_update_matches_full_rebuild(tmp_path):
    from infogrep.retrieval.sparse import SparseIndex

    docs = [
        _passage("a#0", "the quick brown fox jumps legal court", "a.txt"),
        _passage("b#0", "lazy dog sleeps under the legal bench", "b.txt"),
        _passage("c#0", "appeal verdict legal precedent statute court", "c.txt"),
    ]

    # Reference: a full batch build of all three.
    full = SparseIndex(tmp_path / "full" / "sparse", tmp_path / "full" / "cache")
    full.build(iter(docs))

    # Incremental: batch-build the first two, then add the third via IndexWriter.
    inc = SparseIndex(tmp_path / "inc" / "sparse", tmp_path / "inc" / "cache")
    inc.build(iter(docs[:2]))
    added = inc.update(removed_ids=set(), added_passages=iter([docs[2]]))
    assert added == 1

    # Add-only -> no deleted docs skewing stats -> identical ranking AND scores.
    for q in ("legal court", "fox", "appeal verdict", "lazy dog", "legal precedent"):
        f = full.search(q, k=10)
        i = inc.search(q, k=10)
        assert [h.path for h in i] == [h.path for h in f], q
        assert [round(h.score, 4) for h in i] == [round(h.score, 4) for h in f], q


@pytestmark_integration
def test_incremental_delete_and_modify(tmp_path):
    from infogrep.retrieval.sparse import SparseIndex

    si = SparseIndex(tmp_path / "sparse", tmp_path / "cache")
    si.build(iter([
        _passage("a#0", "alpha appletoken legal", "a.txt"),
        _passage("b#0", "beta bananatoken court", "b.txt"),
    ]))

    # Delete b, modify a (new text under the same id; tokens chosen to share no stems).
    si.update(
        removed_ids={"b#0"},
        added_passages=iter([_passage("a#0", "alpha cherrytoken verdict", "a.txt")]),
    )
    assert si.search("bananatoken", k=5) == []            # deleted file gone
    assert not si.search("appletoken", k=5)               # old content of a gone
    assert si.search("cherrytoken", k=5)[0].path == "a.txt"  # modified content present


@pytestmark_integration
def test_incremental_shrink_removes_extra_passages(tmp_path):
    # A file that goes from 3 passages to 1: the extra ids must be deleted.
    from infogrep.retrieval.sparse import SparseIndex

    si = SparseIndex(tmp_path / "sparse", tmp_path / "cache")
    si.build(iter([
        _passage("f#0", "zebra-token first chunk", "f.txt"),
        _passage("f#1", "zebra-token second chunk", "f.txt"),
        _passage("f#2", "zebra-token third chunk", "f.txt"),
    ]))
    assert len(si.search("zebra-token", k=10)) == 3

    # Old ids f#0..f#2 removed; only f#0 re-added.
    si.update(
        removed_ids={"f#0", "f#1", "f#2"},
        added_passages=iter([_passage("f#0", "zebra-token only chunk now", "f.txt")]),
    )
    hits = si.search("zebra-token", k=10)
    assert {h.passage_id for h in hits} == {"f#0"}


@pytestmark_integration
def test_sparse_incremental_via_indexer(tmp_path, monkeypatch):
    from infogrep.config import Config
    from infogrep.indexer import Indexer
    import infogrep.retrieval.sparse as sparse_mod

    (tmp_path / "a.txt").write_text("alpha appletoken legal court")
    (tmp_path / "b.txt").write_text("beta bananatoken statute")
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = False
    Indexer(cfg).reindex()  # full build

    calls = {"build": 0, "update": 0}
    ob, ou = sparse_mod.SparseIndex.build, sparse_mod.SparseIndex.update
    monkeypatch.setattr(sparse_mod.SparseIndex, "build",
                        lambda self, *a, **k: (calls.__setitem__("build", calls["build"] + 1), ob(self, *a, **k))[1])
    monkeypatch.setattr(sparse_mod.SparseIndex, "update",
                        lambda self, *a, **k: (calls.__setitem__("update", calls["update"] + 1), ou(self, *a, **k))[1])

    (tmp_path / "a.txt").write_text("alpha cherrytoken verdict")  # modify
    (tmp_path / "c.txt").write_text("gamma datetoken appeal")      # add
    (tmp_path / "b.txt").unlink()                                  # delete
    Indexer(cfg).reindex()

    assert calls["update"] == 1 and calls["build"] == 0  # incremental, not rebuild

    from infogrep.retrieval.sparse import SparseIndex
    si = SparseIndex(cfg.sparse_dir, cfg.cache_dir)
    assert si.search("cherrytoken", k=5)[0].path == "a.txt"
    assert si.search("datetoken", k=5)[0].path == "c.txt"
    assert si.search("bananatoken", k=5) == []
    assert not si.search("appletoken", k=5)


@pytestmark_integration
def test_index_and_search_end_to_end(tmp_path):
    (tmp_path / "fox.txt").write_text("The quick brown fox jumps over the lazy dog.")
    (tmp_path / "retrieval.md").write_text(
        "Dense and sparse retrieval can be fused with reciprocal rank fusion."
    )
    (tmp_path / "unrelated.txt").write_text("Bananas are a good source of potassium.")

    cfg = Config.load(tmp_path)
    cfg.dense.enabled = False  # isolate sparse from the dense backend
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
    cfg.dense.enabled = False  # isolate sparse from the dense backend
    Indexer(cfg).reindex()

    sparse = SparseIndex(cfg.sparse_dir, cfg.cache_dir)
    assert sparse.search("unique-token-zebra", k=5)

    (tmp_path / "a.txt").unlink()
    Indexer(cfg).reindex()
    # Fresh searcher against the rebuilt index: the deleted doc is gone.
    assert SparseIndex(cfg.sparse_dir, cfg.cache_dir).search("unique-token-zebra", k=5) == []
