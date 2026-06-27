"""Results carry the original file path + file metadata."""

import os

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.indexer import Indexer
from infogrep.retrieval.base import Result, with_file_metadata


def test_with_file_metadata_fills_path_and_stat(tmp_path):
    f = tmp_path / "sub" / "Report Final.pdf"
    f.parent.mkdir()
    f.write_bytes(b"%PDF-1.4 hello")
    r = Result("d", "d#0", "sub/Report Final.pdf", "snip", 1.0, "sparse")
    e = with_file_metadata(r, tmp_path)
    assert e.abs_path == str(tmp_path / "sub" / "Report Final.pdf")
    assert e.filename == "Report Final.pdf"
    assert e.ext == "pdf"
    assert e.size == len(b"%PDF-1.4 hello")
    assert e.mtime is not None


def test_with_file_metadata_missing_file_leaves_stat_none(tmp_path):
    r = Result("d", "d#0", "gone.txt", "snip", 1.0, "sparse")
    e = with_file_metadata(r, tmp_path)
    assert e.filename == "gone.txt" and e.ext == "txt"
    assert e.abs_path == str(tmp_path / "gone.txt")  # path still resolved
    assert e.size is None and e.mtime is None  # stat failed gracefully


def test_to_dict_includes_metadata_keys():
    r = Result("d", "d#0", "a.txt", "s", 1.0, "dense", abs_path="/x/a.txt",
               filename="a.txt", ext="txt", size=5, mtime=1.0)
    d = r.to_dict()
    for key in ("abs_path", "filename", "ext", "size", "mtime"):
        assert key in d
    assert d["abs_path"] == "/x/a.txt"


def test_engine_dense_results_carry_original_path(tmp_path):
    (tmp_path / "berry.txt").write_text("blueberries antioxidants vitamins")
    cfg = Config.load(tmp_path)
    cfg.dense.enabled = True
    cfg.dense.embedder = "hash"
    cfg.sparse.enabled = False
    Indexer(cfg).reindex()

    hits = SearchEngine(cfg).search_dense("antioxidants", k=1)
    assert hits
    h = hits[0]
    assert h.path == "berry.txt"
    assert h.abs_path == str(tmp_path / "berry.txt")
    assert os.path.isfile(h.abs_path)
    assert h.filename == "berry.txt" and h.ext == "txt"
    assert h.size is not None
