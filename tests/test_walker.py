from infogrep.config import Config
from infogrep.ingest.walker import walk


def _make_tree(root):
    (root / "a.txt").write_text("a")
    (root / "sub").mkdir()
    (root / "sub" / "b.md").write_text("b")
    (root / "sub" / "c.log").write_text("c")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x")
    side = root / ".infogrep"
    side.mkdir()
    (side / "manifest.sqlite").write_text("db")


def test_walk_yields_files_and_prunes_noise(tmp_path):
    _make_tree(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.include = ["**/*"]  # test walk mechanics over all file types, not the doc default
    rels = {rel for _, rel in walk(cfg)}
    assert rels == {"a.txt", "sub/b.md", "sub/c.log"}
    # .git and .infogrep are pruned
    assert not any(r.startswith((".git", ".infogrep")) for r in rels)


def test_exclude_pattern(tmp_path):
    _make_tree(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.include = ["**/*"]
    cfg.exclude = cfg.exclude + ["**/*.log"]
    rels = {rel for _, rel in walk(cfg)}
    assert "sub/c.log" not in rels
    assert "a.txt" in rels


def test_include_pattern_restricts(tmp_path):
    _make_tree(tmp_path)
    cfg = Config.load(tmp_path)
    cfg.include = ["**/*.md"]
    rels = {rel for _, rel in walk(cfg)}
    assert rels == {"sub/b.md"}


def test_excluded_directories_are_pruned(tmp_path):
    (tmp_path / "keep.md").write_text("x")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("junk")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.md").write_text("y")
    cfg = Config.load(tmp_path)
    cfg.exclude = cfg.exclude + ["**/node_modules/**", "node_modules/**"]
    rels = {rel for _, rel in walk(cfg)}
    assert "keep.md" in rels and "src/a.md" in rels
    assert not any("node_modules" in r for r in rels)


def test_default_include_is_documents_and_images_not_code(tmp_path):
    (tmp_path / "paper.pdf").write_text("x")
    (tmp_path / "notes.md").write_text("y")
    (tmp_path / "sheet.xlsx").write_text("z")
    (tmp_path / "photo.png").write_text("img")
    (tmp_path / "app.py").write_text("code")        # code: excluded by default
    (tmp_path / "bundle.min.js").write_text("code")  # code: excluded by default
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "readme.md").write_text("dep doc")          # inside node_modules: pruned
    cfg = Config.load(tmp_path)  # defaults
    rels = {rel for _, rel in walk(cfg)}
    assert rels == {"paper.pdf", "notes.md", "sheet.xlsx", "photo.png"}


def test_uppercase_extensions_are_indexed(tmp_path):
    """Extensions match regardless of case.

    macOS and Windows filesystems don't distinguish PHOTO.PNG from photo.png, so a
    config listing "**/*.png" has to pick up both — otherwise cameras and scanners,
    which routinely emit .JPG/.HEIC, produce files that are silently never indexed.
    """
    (tmp_path / "photo.PNG").write_text("img")
    (tmp_path / "scan.JPG").write_text("img")
    (tmp_path / "invoice.HEIC").write_text("img")
    (tmp_path / "paper.PDF").write_text("doc")
    (tmp_path / "notes.Md").write_text("doc")
    (tmp_path / "lower.png").write_text("img")
    cfg = Config.load(tmp_path)  # defaults, all-lowercase patterns
    rels = {rel for _, rel in walk(cfg)}
    assert rels == {"photo.PNG", "scan.JPG", "invoice.HEIC", "paper.PDF",
                    "notes.Md", "lower.png"}


def test_uppercase_names_keep_their_real_case(tmp_path):
    """Matching is case-insensitive, but the path yielded is the real one on disk."""
    (tmp_path / "MyDoc.PDF").write_text("x")
    cfg = Config.load(tmp_path)
    assert [rel for _, rel in walk(cfg)] == ["MyDoc.PDF"]


def test_exclude_is_case_insensitive_too(tmp_path):
    """Include and exclude must agree on case, or an exclude silently stops working."""
    (tmp_path / "keep.md").write_text("x")
    (tmp_path / "DRAFT.MD").write_text("y")
    cfg = Config.load(tmp_path)
    cfg.exclude = [*cfg.exclude, "**/draft.md"]
    rels = {rel for _, rel in walk(cfg)}
    assert rels == {"keep.md"}


def test_uppercase_extension_survives_a_full_index(tmp_path):
    """End to end: an uppercase-suffixed file is indexed and stays out of staleness."""
    from infogrep.indexer import Indexer

    (tmp_path / "photo.PNG").write_text("img")
    (tmp_path / "paper.TXT").write_text("hello uppercase world")
    cfg = Config.load(tmp_path)
    cfg.sparse.enabled = False
    cfg.dense.enabled = False
    report = Indexer(cfg).reindex()

    assert report.added == 2
    assert Indexer(cfg).status()["pending"] == 0
