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
    assert not any(r.startswith(".git") or r.startswith(".infogrep") for r in rels)


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
