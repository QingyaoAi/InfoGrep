import time

from infogrep.config import Config
from infogrep.indexer import Indexer


def _cfg(tmp_path):
    """M1 tests exercise manifest/change-detection only — skip the heavy backends."""
    cfg = Config.load(tmp_path)
    cfg.sparse.enabled = False
    cfg.dense.enabled = False
    return cfg


def _corpus(root):
    (root / "notes.md").write_text("# Title\n\nThe quick brown fox jumps over the lazy dog.")
    (root / "readme.txt").write_text("InfoGrep indexes local files for coding agents.")
    (root / "code.py").write_text("def hello():\n    return 'world'\n")
    (root / "binary.bin").write_bytes(b"\x00\x01\x02\x03")


def test_index_then_noop_then_change(tmp_path):
    _corpus(tmp_path)
    idx = Indexer(_cfg(tmp_path))

    # First run: everything is added; passages produced.
    r1 = idx.reindex()
    assert r1.added == 3  # md, txt, py (bin is unsupported -> skipped)
    assert r1.skipped == 1
    assert r1.modified == 0 and r1.deleted == 0
    assert r1.n_files == 3
    assert r1.n_passages >= 3

    # Second run with no changes: pure no-op.
    r2 = idx.reindex()
    assert r2.added == 0 and r2.modified == 0 and r2.deleted == 0
    assert r2.unchanged == 3
    assert r2.n_passages == r1.n_passages

    # Modify one file -> exactly one modified.
    time.sleep(0.01)
    (tmp_path / "notes.md").write_text("# Title\n\nCompletely different content here now.")
    r3 = idx.reindex()
    assert r3.modified == 1 and r3.added == 0
    assert r3.unchanged == 2

    # Delete one file -> exactly one deleted, removed from the manifest.
    (tmp_path / "readme.txt").unlink()
    r4 = idx.reindex()
    assert r4.deleted == 1
    assert r4.n_files == 2


def test_full_reindex_reprocesses_all(tmp_path):
    _corpus(tmp_path)
    idx = Indexer(_cfg(tmp_path))
    idx.reindex()
    r = idx.reindex(full=True)
    assert r.modified == 3 and r.unchanged == 0


def test_status_reflects_index(tmp_path):
    _corpus(tmp_path)
    cfg = _cfg(tmp_path)
    assert Indexer(cfg).status() == {"indexed": False}
    Indexer(cfg).reindex()
    info = Indexer(cfg).status()
    assert info["indexed"] is True
    assert info["n_files"] == 3
    assert info["index_version"] >= 1
