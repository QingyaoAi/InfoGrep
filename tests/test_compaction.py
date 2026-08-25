"""Deleting files must release their disk space, not just hide them from results.

Both backends tombstone rather than free: Lucene leaves deleted documents in place until
a merge rewrites the segment, and SQLite parks freed pages on a freelist. Without an
explicit compaction step an index that churns grows without bound.
"""

import pytest

from infogrep.config import Config
from infogrep.indexer import Indexer
from infogrep.manifest import Manifest
from infogrep.retrieval.sparse import SparseIndex


def _sparse_available() -> bool:
    try:
        from infogrep import anserini

        return anserini.available()
    except Exception:
        return False


needs_sparse = pytest.mark.skipif(
    not _sparse_available(), reason="Anserini jar/JDK21 not available"
)


def _cfg(tmp_path, sparse=False):
    cfg = Config.load(tmp_path)
    cfg.sparse.enabled = sparse
    cfg.dense.enabled = False
    return cfg


def _populate(tmp_path, n, words=400):
    for i in range(n):
        (tmp_path / f"f{i:03d}.txt").write_text(" ".join(f"token{i}x{w}" for w in range(words)))


def test_manifest_vacuum_reclaims_freed_pages(tmp_path):
    _populate(tmp_path, 40)
    cfg = _cfg(tmp_path)
    Indexer(cfg).reindex()

    for i in range(35):  # drop most of the corpus
        (tmp_path / f"f{i:03d}.txt").unlink()
    report = Indexer(cfg).reindex()

    assert report.deleted == 35
    assert report.compacted_manifest is True
    with Manifest(cfg.manifest_path) as m:
        assert m.free_ratio() < 0.2  # freelist actually returned to the OS


def test_compaction_is_skipped_when_little_is_dead(tmp_path):
    _populate(tmp_path, 40)
    cfg = _cfg(tmp_path)
    Indexer(cfg).reindex()

    (tmp_path / "f000.txt").unlink()  # one file out of 40 -> not worth a rewrite
    report = Indexer(cfg).reindex()

    assert report.deleted == 1
    assert report.compacted_manifest is False


def test_compaction_does_not_run_when_nothing_is_dead(tmp_path):
    """An add-only run has no waste to reclaim, so it must not pay for a rewrite."""
    _populate(tmp_path, 10)
    cfg = _cfg(tmp_path)
    Indexer(cfg).reindex()

    (tmp_path / "new.txt").write_text("an added file, nothing removed")
    report = Indexer(cfg).reindex()

    assert report.deleted == 0
    assert report.compacted_manifest is False
    assert report.compacted_sparse is False


def test_accumulated_waste_is_reclaimed_even_on_a_quiet_run(tmp_path):
    """Waste belongs to the index, not to the run that created it.

    A directory that lost many files while compaction was off must still get its space
    back on a later reindex, even one that deletes nothing itself.
    """
    _populate(tmp_path, 40)
    cfg = _cfg(tmp_path)
    cfg.compact.enabled = False
    Indexer(cfg).reindex()
    for i in range(35):
        (tmp_path / f"f{i:03d}.txt").unlink()
    Indexer(cfg).reindex()
    with Manifest(cfg.manifest_path) as m:
        assert m.free_ratio() >= 0.2  # waste is sitting there

    cfg.compact.enabled = True
    report = Indexer(cfg).reindex()  # nothing added, nothing deleted

    assert report.deleted == 0
    assert report.compacted_manifest is True
    with Manifest(cfg.manifest_path) as m:
        assert m.free_ratio() < 0.2


def test_compaction_can_be_disabled(tmp_path):
    _populate(tmp_path, 40)
    cfg = _cfg(tmp_path)
    cfg.compact.enabled = False
    Indexer(cfg).reindex()

    for i in range(35):
        (tmp_path / f"f{i:03d}.txt").unlink()
    report = Indexer(cfg).reindex()

    assert report.deleted == 35
    assert report.compacted_manifest is False
    with Manifest(cfg.manifest_path) as m:
        assert m.free_ratio() >= 0.2  # left untouched, as configured


def test_deleted_content_stays_searchable_nowhere_after_compaction(tmp_path):
    """Compaction must not resurrect or lose anything: survivors stay, deletions go."""
    _populate(tmp_path, 40)
    cfg = _cfg(tmp_path)
    Indexer(cfg).reindex()

    for i in range(35):
        (tmp_path / f"f{i:03d}.txt").unlink()
    Indexer(cfg).reindex()

    with Manifest(cfg.manifest_path) as m:
        paths = m.all_paths()
    assert paths == {f"f{i:03d}.txt" for i in range(35, 40)}


@needs_sparse
def test_sparse_compaction_is_driven_by_the_tombstone_ratio(tmp_path, monkeypatch):
    """The Indexer compacts sparse exactly when enough of the index is tombstoned.

    Lucene reclaims deletes on its own whenever it happens to merge, so a small index
    never accumulates tombstones. A large multi-segment one does: TieredMergePolicy
    will not rewrite big segments just to drop deleted documents. The ratio is stubbed
    here so the decision is tested without building a multi-gigabyte index.
    """
    from infogrep.retrieval import sparse as sparse_mod

    _populate(tmp_path, 20)
    cfg = _cfg(tmp_path, sparse=True)
    Indexer(cfg).reindex()

    calls = []
    monkeypatch.setattr(sparse_mod.SparseIndex, "compact", lambda self: calls.append(1))

    monkeypatch.setattr(sparse_mod.SparseIndex, "deleted_ratio", lambda self: 0.35)
    (tmp_path / "f000.txt").unlink()
    assert Indexer(cfg).reindex().compacted_sparse is True
    assert len(calls) == 1

    monkeypatch.setattr(sparse_mod.SparseIndex, "deleted_ratio", lambda self: 0.05)
    (tmp_path / "f001.txt").unlink()
    assert Indexer(cfg).reindex().compacted_sparse is False
    assert len(calls) == 1  # below threshold -> no rewrite


@needs_sparse
def test_sparse_compact_clears_tombstones_and_keeps_survivors(tmp_path):
    """compact() itself: tombstones go, live documents remain retrievable."""
    _populate(tmp_path, 40)
    cfg = _cfg(tmp_path, sparse=True)
    cfg.compact.enabled = False  # drive compact() by hand
    Indexer(cfg).reindex()

    for i in range(30):
        (tmp_path / f"f{i:03d}.txt").unlink()
    Indexer(cfg).reindex()

    sparse = SparseIndex(cfg.sparse_dir, cfg.cache_dir, language=cfg.sparse.language)
    sparse.compact()  # idempotent even when Lucene already merged the deletes away
    assert sparse.deleted_ratio() == 0.0

    fresh = SparseIndex(cfg.sparse_dir, cfg.cache_dir, language=cfg.sparse.language)
    assert fresh.search("token35x3", k=5)      # survivor
    assert fresh.search("token0x3", k=5) == []  # deleted
