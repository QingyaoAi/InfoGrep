import json
import shutil
import subprocess

import pytest

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.retrieval.kb import KnowledgeBaseIndex, ObsidianCliError


class FakeObsidian:
    """Stand-in for the Obsidian CLI: returns canned JSON for each subcommand."""

    def __init__(self, search=None, links=None, backlinks=None, contents=None, error=None):
        self.search = search or []
        self.links = links or {}
        self.backlinks = backlinks or {}
        self.contents = contents or {}
        self.error = error
        self.calls = []

    def __call__(self, command, params):
        self.calls.append((command, params))
        if self.error:
            return self.error
        if command == "search":
            return json.dumps(self.search)
        if command == "links":
            return json.dumps(self.links.get(params["path"], []))
        if command == "backlinks":
            return json.dumps(self.backlinks.get(params["path"], []))
        if command == "read":
            return self.contents.get(params["path"], "")
        return ""


def _cfg(tmp_path, **kb):
    cfg = Config.load(tmp_path)
    cfg.kb.enabled = True
    cfg.sparse.enabled = False
    cfg.dense.enabled = False
    cfg.graph.enabled = False
    for key, val in kb.items():
        setattr(cfg.kb, key, val)
    return cfg


def test_parse_paths_tolerates_json_lines_and_filters_non_md(tmp_path):
    kb = KnowledgeBaseIndex(_cfg(tmp_path), runner=FakeObsidian())
    assert kb._parse_paths('["a.md", "b/c.md", "d.base"]') == ["a.md", "b/c.md"]
    assert kb._parse_paths("a.md\nx.png\nb.md") == ["a.md", "b.md"]
    assert kb._parse_paths("No results found.") == []
    assert kb._parse_paths("Error: something") == []


def test_search_ranks_by_search_order(tmp_path):
    fake = FakeObsidian(search=["First.md", "Second.md"], contents={"First.md": "alpha"})
    kb = KnowledgeBaseIndex(_cfg(tmp_path, hops=0), runner=fake)
    hits = kb.search("alpha", k=5)
    assert [h.path for h in hits] == ["First.md", "Second.md"]
    assert hits[0].score > hits[1].score
    assert hits[0].retriever == "kb"


def test_graph_expansion_follows_links_and_backlinks(tmp_path):
    # Query matches only Attention; hops=1 pulls in its outgoing link (Positional
    # Encoding) AND its backlink (Transformers), neither of which match the query.
    fake = FakeObsidian(
        search=["Attention.md"],
        links={"Attention.md": ["Positional Encoding.md"]},
        backlinks={"Attention.md": ["Transformers.md"]},
        contents={"Attention.md": "softmax over scores"},
    )
    cfg = _cfg(tmp_path, hops=1)
    paths = {h.path for h in KnowledgeBaseIndex(cfg, runner=fake).search("softmax", k=10)}
    assert paths == {"Attention.md", "Positional Encoding.md", "Transformers.md"}

    cfg0 = _cfg(tmp_path, hops=0)
    paths0 = {h.path for h in KnowledgeBaseIndex(cfg0, runner=fake).search("softmax", k=10)}
    assert paths0 == {"Attention.md"}


def test_snippet_strips_frontmatter_and_centers_on_match(tmp_path):
    content = "---\ntitle: A\ntags: []\n---\nIntro line. The softmax normalizes scores nicely."
    fake = FakeObsidian(search=["A.md"], contents={"A.md": content})
    kb = KnowledgeBaseIndex(_cfg(tmp_path, hops=0), runner=fake)
    snip = kb.search("softmax", k=1)[0].snippet
    assert "title: A" not in snip  # frontmatter removed
    assert "softmax" in snip


def test_app_down_raises(tmp_path):
    fake = FakeObsidian(error="Error: please make sure Obsidian is running")
    kb = KnowledgeBaseIndex(_cfg(tmp_path), runner=fake)
    with pytest.raises(ObsidianCliError):
        kb.search("anything", k=3)
    assert isinstance(ObsidianCliError("x"), FileNotFoundError)


def test_hybrid_skips_kb_when_app_down(tmp_path):
    cfg = _cfg(tmp_path)
    engine = SearchEngine(cfg)
    engine._backends["kb"] = KnowledgeBaseIndex(
        cfg, runner=FakeObsidian(error="Error: make sure Obsidian is running")
    )
    out = engine.search_hybrid("x", k=3, retrievers=["kb"])
    assert out.used == []
    assert "kb" in out.skipped


def test_hybrid_includes_kb(tmp_path):
    cfg = _cfg(tmp_path)
    engine = SearchEngine(cfg)
    engine._backends["kb"] = KnowledgeBaseIndex(
        cfg, runner=FakeObsidian(search=["Note.md"], contents={"Note.md": "hello world"})
    )
    out = engine.search_hybrid("hello", k=3)
    assert out.used == ["kb"]
    assert out.results and out.results[0].retriever == "hybrid"


# -- guarded integration test against the real Obsidian CLI -----------------

def _obsidian_live() -> bool:
    if not shutil.which("obsidian"):
        return False
    try:
        out = subprocess.run(
            ["obsidian", "vault"], capture_output=True, text=True, timeout=15
        ).stdout.lower()
        return "error" not in out and bool(out.strip())
    except Exception:
        return False


@pytest.mark.skipif(not _obsidian_live(), reason="Obsidian CLI/app not available")
def test_real_cli_search_runs(tmp_path):
    cfg = _cfg(tmp_path, hops=0)
    hits = KnowledgeBaseIndex(cfg).search("the", k=3)
    assert isinstance(hits, list)
    for h in hits:
        assert h.path.lower().endswith(".md")
        assert h.retriever == "kb"
