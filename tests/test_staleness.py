from infogrep.config import Config
from infogrep.indexer import Indexer


def _cfg(tmp_path):
    cfg = Config.load(tmp_path)
    cfg.sparse.enabled = False
    cfg.dense.enabled = False
    return cfg


def test_status_reports_staleness(tmp_path):
    (tmp_path / "a.txt").write_text("alpha content")
    (tmp_path / "b.txt").write_text("beta content")
    cfg = _cfg(tmp_path)
    Indexer(cfg).reindex()

    fresh = Indexer(cfg).status()
    assert fresh["stale"] is False
    assert fresh["pending"] == 0

    # Add, modify, delete -> all three reflected without reindexing.
    (tmp_path / "c.txt").write_text("gamma new file")
    (tmp_path / "a.txt").write_text("alpha CHANGED content")
    (tmp_path / "b.txt").unlink()

    stale = Indexer(cfg).status()
    assert stale["stale"] is True
    assert stale["pending"] == 3
    assert stale["pending_added"] == 1
    assert stale["pending_modified"] == 1
    assert stale["pending_deleted"] == 1

    # After reindex, staleness clears.
    Indexer(cfg).reindex()
    assert Indexer(cfg).status()["pending"] == 0


def test_status_can_skip_staleness(tmp_path):
    (tmp_path / "a.txt").write_text("alpha")
    cfg = _cfg(tmp_path)
    Indexer(cfg).reindex()
    info = Indexer(cfg).status(check_staleness=False)
    assert "stale" not in info
    assert info["indexed"] is True


def test_name_only_files_are_not_phantom_deletions(tmp_path):
    """Files indexed by name/path only must not count as pending deletions.

    ``reindex`` indexes every walked file, falling back to a name/path-only stub when
    no content extractor matches the suffix. Staleness has to use the same walk, or
    those files show up as deletions on every ``status`` and never clear.
    """
    (tmp_path / "a.txt").write_text("alpha content")
    (tmp_path / "diagram.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    cfg = _cfg(tmp_path)
    cfg.include = ["**/*"]
    Indexer(cfg).reindex()

    info = Indexer(cfg).status()
    assert info["pending_deleted"] == 0
    assert info["pending"] == 0
    assert info["stale"] is False


def test_name_only_file_deletion_is_still_detected(tmp_path):
    """The converse: a genuinely removed name-only file is still reported."""
    (tmp_path / "diagram.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    cfg = _cfg(tmp_path)
    cfg.include = ["**/*"]
    Indexer(cfg).reindex()

    (tmp_path / "diagram.svg").unlink()
    info = Indexer(cfg).status()
    assert info["pending_deleted"] == 1
    assert info["stale"] is True
