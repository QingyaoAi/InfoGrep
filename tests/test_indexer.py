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

    # First run: all 4 files added; bin has no content so it's indexed by name only.
    r1 = idx.reindex()
    assert r1.added == 4  # md, txt, py, bin (bin is name-only)
    assert r1.name_only == 1  # binary.bin -> stub passage (findable by filename)
    assert r1.modified == 0 and r1.deleted == 0
    assert r1.n_files == 4
    assert r1.n_passages >= 4

    # Second run with no changes: pure no-op.
    r2 = idx.reindex()
    assert r2.added == 0 and r2.modified == 0 and r2.deleted == 0
    assert r2.unchanged == 4
    assert r2.n_passages == r1.n_passages

    # Modify one file -> exactly one modified.
    time.sleep(0.01)
    (tmp_path / "notes.md").write_text("# Title\n\nCompletely different content here now.")
    r3 = idx.reindex()
    assert r3.modified == 1 and r3.added == 0
    assert r3.unchanged == 3  # readme.txt, code.py, binary.bin

    # Delete one file -> exactly one deleted, removed from the manifest.
    (tmp_path / "readme.txt").unlink()
    r4 = idx.reindex()
    assert r4.deleted == 1
    assert r4.n_files == 3  # notes.md, code.py, binary.bin


def _snapshot(root):
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
    }


def test_indexing_does_not_touch_target_and_index_is_separate(tmp_path):
    (tmp_path / "a.txt").write_text("hello world")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("more text here")
    cfg = _cfg(tmp_path)

    before = _snapshot(tmp_path)
    Indexer(cfg).reindex()
    after = _snapshot(tmp_path)

    # The target folder is completely unchanged (no new files, no mtime/size changes).
    assert before == after
    assert not (tmp_path / ".infogrep").exists()
    # The index lives in a separate location and is populated there.
    assert not cfg.index_dir.is_relative_to(tmp_path.resolve())
    assert cfg.manifest_path.exists()


def test_indexing_works_on_read_only_folder(tmp_path):
    import os
    import stat

    (tmp_path / "doc.txt").write_text("read only content about retrieval")
    cfg = _cfg(tmp_path)
    # Make the whole target tree read-only; indexing must still succeed (reads only).
    for p in [tmp_path, *tmp_path.rglob("*")]:
        os.chmod(p, stat.S_IREAD | stat.S_IEXEC)
    try:
        report = Indexer(cfg).reindex()
        assert report.added == 1 and not report.errors
        assert cfg.manifest_path.exists()
    finally:
        for p in [tmp_path, *tmp_path.rglob("*")]:
            os.chmod(p, stat.S_IRWXU)


def test_full_reindex_reprocesses_all(tmp_path):
    _corpus(tmp_path)
    idx = Indexer(_cfg(tmp_path))
    idx.reindex()
    r = idx.reindex(full=True)
    assert r.modified == 4 and r.unchanged == 0  # all 4 files reprocessed


def test_status_reflects_index(tmp_path):
    _corpus(tmp_path)
    cfg = _cfg(tmp_path)
    assert Indexer(cfg).status() == {"indexed": False}
    Indexer(cfg).reindex()
    info = Indexer(cfg).status()
    assert info["indexed"] is True
    assert info["n_files"] == 4  # incl. binary.bin (name-only)
    assert info["index_version"] >= 1
