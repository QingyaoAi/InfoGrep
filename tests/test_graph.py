"""Tests for the folder/filename metadata knowledge graph (no file content involved)."""

import json

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.indexer import Indexer
from infogrep.ingest.graph import build_folder_tree, build_graph
from infogrep.retrieval.graph import FolderGraphIndex


def _paths():
    return {
        "readme.txt",
        "Taxes/2024/w2.pdf",
        "Taxes/2024/receipts.csv",
        "Taxes/2023/w2.pdf",
        "Recipes/pasta.md",
    }


def test_build_folder_tree_captures_hierarchy_and_files():
    tree = build_folder_tree(_paths())

    assert tree[""]["files"] == ["readme.txt"]
    assert tree[""]["subfolders"] == {"Taxes", "Recipes"}
    assert tree[""]["parent"] is None

    assert tree["Taxes"]["subfolders"] == {"Taxes/2024", "Taxes/2023"}
    assert tree["Taxes"]["parent"] == ""
    assert tree["Taxes"]["files"] == []

    assert set(tree["Taxes/2024"]["files"]) == {"w2.pdf", "receipts.csv"}
    assert tree["Taxes/2024"]["parent"] == "Taxes"


def test_build_graph_writes_vault_and_json(tmp_path):
    index_dir = tmp_path / "idx"
    build_graph(index_dir, _paths())

    # JSON side: fast machine-readable form.
    graph = json.loads((index_dir / "graph.json").read_text())
    assert set(graph) == {"", "Taxes", "Taxes/2024", "Taxes/2023", "Recipes"}
    assert graph["Taxes/2024"]["parent"] == "Taxes"
    assert "receipts" in graph["Taxes/2024"]["file_tokens"]
    assert "taxes" in graph["Taxes"]["name_tokens"]

    # Vault side: an Obsidian-compatible note per folder, wikilinked to its parent.
    vault = index_dir / "graph_vault"
    assert (vault / "_root.md").is_file()
    assert (vault / "Taxes.md").is_file()
    assert (vault / "Taxes" / "2024.md").is_file()

    taxes_note = (vault / "Taxes.md").read_text()
    assert "[[_root]]" in taxes_note  # parent link
    assert "[[Taxes/2024]]" in taxes_note  # subfolder link

    root_note = (vault / "_root.md").read_text()
    assert "readme.txt" in root_note
    assert "[[Taxes]]" in root_note
    assert "[[Recipes]]" in root_note


def test_build_graph_regenerates_cleanly_when_files_move(tmp_path):
    index_dir = tmp_path / "idx"
    build_graph(index_dir, _paths())
    build_graph(index_dir, {"Recipes/pasta.md", "readme.txt"})  # Taxes/* removed

    graph = json.loads((index_dir / "graph.json").read_text())
    assert set(graph) == {"", "Recipes"}
    vault = index_dir / "graph_vault"
    assert not (vault / "Taxes.md").exists()
    assert not (vault / "Taxes").exists()


def test_folder_graph_index_matches_folder_name_and_returns_files(tmp_path):
    build_graph(tmp_path, _paths())
    idx = FolderGraphIndex(tmp_path, hops=0, max_folders=5)

    hits = idx.search("taxes", k=10)
    paths = {h.path for h in hits}
    # All files under Taxes/ (both years) should surface, not just an exact-name match.
    assert paths == {"Taxes/2024/w2.pdf", "Taxes/2024/receipts.csv", "Taxes/2023/w2.pdf"}
    assert all(h.retriever == "graph" for h in hits)


def test_folder_graph_index_no_duplicates_when_folders_overlap(tmp_path):
    # With hops>0, "Taxes" (recursively covering both years) and its own children
    # "Taxes/2023"/"Taxes/2024" all score and land in the same expanded folder set --
    # each file must still be reported only once.
    build_graph(tmp_path, _paths())
    idx = FolderGraphIndex(tmp_path, hops=1, max_folders=5)

    hits = idx.search("taxes", k=10)
    paths = [h.path for h in hits]
    assert len(paths) == len(set(paths))


def test_folder_graph_index_expands_via_hops(tmp_path):
    build_graph(tmp_path, _paths())
    # hops=0: only the folder literally named "2024" matches "2024".
    idx0 = FolderGraphIndex(tmp_path, hops=0, max_folders=5)
    assert {h.path for h in idx0.search("2024", k=10)} == {
        "Taxes/2024/w2.pdf", "Taxes/2024/receipts.csv",
    }
    # hops=1: expands to the sibling folder "2023" (same parent "Taxes").
    idx1 = FolderGraphIndex(tmp_path, hops=1, max_folders=5)
    paths1 = {h.path for h in idx1.search("2024", k=10)}
    assert "Taxes/2023/w2.pdf" in paths1


def test_folder_graph_index_no_match_returns_empty(tmp_path):
    build_graph(tmp_path, _paths())
    idx = FolderGraphIndex(tmp_path, hops=1, max_folders=5)
    assert idx.search("nonexistent_zzz", k=10) == []


def test_folder_graph_index_missing_graph_json_returns_empty(tmp_path):
    idx = FolderGraphIndex(tmp_path / "does_not_exist", hops=1, max_folders=5)
    assert idx.search("anything", k=10) == []


def test_indexer_builds_graph_by_default(tmp_path):
    (tmp_path / "Recipes").mkdir()
    (tmp_path / "Recipes" / "pasta.md").write_text("Boil water. Add pasta.")
    (tmp_path / "Recipes" / "soup.md").write_text("Simmer stock.")

    cfg = Config.load(tmp_path)
    cfg.sparse.enabled = False  # keep the test JVM-free; graph itself needs no backend
    Indexer(cfg).reindex()

    assert cfg.graph_json_path.is_file()
    graph = json.loads(cfg.graph_json_path.read_text())
    assert set(graph["Recipes"]["files"]) == {"pasta.md", "soup.md"}


def test_engine_search_graph_is_enriched_with_file_metadata(tmp_path):
    (tmp_path / "Recipes").mkdir()
    (tmp_path / "Recipes" / "pasta.md").write_text("Boil water. Add pasta.")
    (tmp_path / "Recipes" / "soup.md").write_text("Simmer stock.")

    cfg = Config.load(tmp_path)
    cfg.sparse.enabled = False
    Indexer(cfg).reindex()

    hits = SearchEngine(cfg).search_graph("recipes", k=10)
    assert {h.path for h in hits} == {"Recipes/pasta.md", "Recipes/soup.md"}
    assert all(h.abs_path == str(tmp_path / h.path) for h in hits)


def test_hybrid_includes_graph_by_default(tmp_path):
    (tmp_path / "Recipes").mkdir()
    (tmp_path / "Recipes" / "pasta.md").write_text("Boil water. Add pasta.")
    (tmp_path / "Recipes" / "soup.md").write_text("Simmer stock.")

    cfg = Config.load(tmp_path)
    cfg.sparse.enabled = False
    cfg.kb.enabled = False
    Indexer(cfg).reindex()

    out = SearchEngine(cfg).search_hybrid("recipes", k=10)
    assert out.used == ["graph"]
    assert {r.path for r in out.results} == {"Recipes/pasta.md", "Recipes/soup.md"}
