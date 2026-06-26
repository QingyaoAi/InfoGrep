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
