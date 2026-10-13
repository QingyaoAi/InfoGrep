import pytest

from infogrep.config import DEFAULT_DOC_TYPES, DEFAULT_IMAGE_TYPES, Config
from infogrep.engine import FILE_TYPES, SearchEngine, search_across
from infogrep.indexer import Indexer


def _corpus(root):
    (root / "fox.txt").write_text("The quick brown fox jumps over the lazy dog.")
    (root / "retrieval.md").write_text(
        "Dense and sparse retrieval fused with reciprocal rank fusion."
    )
    (root / "banana.txt").write_text("Bananas are a good source of potassium and fiber.")


def _sparse_available() -> bool:
    try:
        from infogrep import anserini

        return anserini.available()
    except Exception:
        return False


needs_sparse = pytest.mark.skipif(not _sparse_available(), reason="Anserini jar/JDK21 not available")


def test_hybrid_default_runs_only_enabled(tmp_path):
    # Default retriever set = enabled ones only; disabled sparse simply isn't run.
    _corpus(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = True
    cfg.dense.embedder = "hash"
    cfg.sparse.enabled = False
    cfg.kb.enabled = False
    cfg.graph.enabled = False
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
    cfg.graph.enabled = False
    Indexer(cfg).reindex()

    out = SearchEngine(cfg).search_hybrid("reciprocal rank fusion retrieval", k=3)
    assert set(out.used) == {"sparse", "dense"}
    assert out.results
    # The doc both retrievers agree on should surface at the top.
    assert out.results[0].path == "retrieval.md"
    # No duplicate passages after fusion.
    keys = [(r.doc_id, r.passage_id) for r in out.results]
    assert len(keys) == len(set(keys))


def _light_engine(root, files):
    """An indexed directory searched without the JVM: hash embeddings + folder graph."""
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text)
    cfg = Config.load(root)
    cfg.sparse.enabled = False
    cfg.dense.enabled = True
    cfg.dense.embedder = "hash"
    Indexer(cfg).reindex()
    return SearchEngine(cfg)


def test_search_across_pools_directories(tmp_path):
    work = _light_engine(tmp_path / "work", {
        "notes.txt": "quarterly budget review for the retrieval project",
        "fox.txt": "the quick brown fox jumps over the lazy dog",
    })
    home = _light_engine(tmp_path / "home", {
        "notes.txt": "passport renewal appointment and visa photos",
    })

    out = search_across([work, home], "hybrid", "passport renewal appointment", k=5)
    assert set(out.used) == {"dense", "graph"}
    # Both directories have a notes.txt (identical ids); they stay two results, each
    # pointing into its own directory, and the one that matches leads.
    paths = [r.abs_path for r in out.results]
    assert paths[0] == str(tmp_path / "home" / "notes.txt")
    assert str(tmp_path / "work" / "notes.txt") in paths
    assert len(paths) == len(set(paths))

    only_dense = search_across([work, home], "dense", "quick brown fox", k=1)
    assert only_dense.used == ["dense"]
    assert only_dense.results[0].abs_path == str(tmp_path / "work" / "fox.txt")

    # A single directory is just that directory's own search.
    alone = search_across([home], "hybrid", "passport", k=5)
    assert alone.results == home.search("hybrid", "passport", k=5).results


def test_search_across_reports_a_retriever_that_fails_everywhere(tmp_path):
    work = _light_engine(tmp_path / "work", {"a.txt": "alpha"})
    home = _light_engine(tmp_path / "home", {"b.txt": "beta"})
    for engine in (work, home):
        engine.config.sparse.enabled = True  # enabled, but no sparse index was built

    out = search_across([work, home], "hybrid", "alpha", k=3)
    assert "sparse" in out.skipped and "sparse" not in out.used
    assert out.results  # the other retrievers still answer
    with pytest.raises(FileNotFoundError):
        search_across([work, home], "sparse", "alpha", k=3)


@needs_sparse
def test_search_across_sparse_points_each_hit_at_its_own_directory(tmp_path):
    engines = []
    for name, text in (("work", "zebra crossing design notes"), ("home", "zebra enclosure tickets")):
        root = tmp_path / name
        root.mkdir()
        (root / "notes.txt").write_text(text)
        cfg = Config.load(root)
        cfg.graph.enabled = False
        Indexer(cfg).reindex()
        engines.append(SearchEngine(cfg))

    out = search_across(engines, "hybrid", "zebra", k=5)
    assert out.used == ["sparse"]
    assert {r.abs_path for r in out.results} == {
        str(tmp_path / "work" / "notes.txt"), str(tmp_path / "home" / "notes.txt"),
    }
    top = search_across(engines, "sparse", "zebra enclosure", k=1).results[0]
    assert top.abs_path == str(tmp_path / "home" / "notes.txt")


def test_file_types_cover_everything_indexed_by_default():
    # A default-indexed extension outside every group could never be filtered for.
    grouped = [ext for _, exts in FILE_TYPES.values() for ext in exts]
    assert len(grouped) == len(set(grouped))  # each extension belongs to one group
    assert set(DEFAULT_DOC_TYPES + DEFAULT_IMAGE_TYPES) <= set(grouped)


def test_type_filter_through_hybrid_and_across_directories(tmp_path):
    work = _light_engine(tmp_path / "work", {
        "plan.txt": "zebra migration plan",
        "plan.md": "zebra migration plan",
    })
    home = _light_engine(tmp_path / "home", {"zoo.md": "zebra enclosure tickets"})

    one = work.search("hybrid", "zebra migration plan", k=5, exts=("md",))
    assert [r.path for r in one.results] == ["plan.md"]
    assert [r.ext for r in work.search("dense", "zebra", k=5, exts=("txt",)).results] == ["txt"]

    both = search_across([work, home], "hybrid", "zebra", k=5, exts=("md",))
    assert {r.abs_path for r in both.results} == {
        str(tmp_path / "work" / "plan.md"), str(tmp_path / "home" / "zoo.md"),
    }
