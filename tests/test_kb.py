"""The knowledge-base retriever, against vaults made of real files: no Obsidian needed."""

import json
import os

import pytest

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.retrieval import kb as kb_module
from infogrep.retrieval.kb import KnowledgeBaseIndex, VaultUnavailable, vault_root


def _vault(tmp_path, notes):
    root = tmp_path / "vault"
    for path, text in notes.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    (root / ".obsidian").mkdir(parents=True, exist_ok=True)
    return root


def _cfg(tmp_path, vault=None, **kb):
    cfg = Config.load(tmp_path)
    cfg.kb.enabled = True
    cfg.kb.vault_path = str(vault) if vault else None
    cfg.sparse.enabled = False
    cfg.dense.enabled = False
    cfg.graph.enabled = False
    for key, val in kb.items():
        setattr(cfg.kb, key, val)
    return cfg


def _kb(tmp_path, notes, **kb):
    return KnowledgeBaseIndex(_cfg(tmp_path, _vault(tmp_path, notes), **kb))


def _paths(hits):
    return [h.path for h in hits]


# -- searching ----------------------------------------------------------------------


def test_search_ranks_by_relevance_with_titles_weighted_up(tmp_path):
    kb = _kb(tmp_path, {
        "Softmax.md": "A short note.",
        "Attention.md": "Attention puts a softmax over the scores; softmax again.",
        "Other.md": "Nothing to see.",
    }, hops=0)
    hits = kb.search("softmax", k=5)
    assert _paths(hits) == ["Softmax.md", "Attention.md"]  # the title outweighs the text
    assert hits[0].score > hits[1].score and hits[0].retriever == "kb"


def test_chinese_matches_by_character_pairs(tmp_path):
    kb = _kb(tmp_path, {
        "项目.md": "本项目研究信息检索与排序学习。",
        "生活.md": "周末去爬山。",
    }, hops=0)
    assert _paths(kb.search("信息检索", k=5)) == ["项目.md"]
    assert _paths(kb.search("检索", k=5)) == ["项目.md"]
    assert kb.search("检索", k=1)[0].snippet.startswith("本项目研究信息检索")


def test_an_unfinished_last_word_matches_the_words_it_begins(tmp_path):
    # Search as you type: "alp" is "alpha" on its way.
    kb = _kb(tmp_path, {"A.md": "alpha particles", "B.md": "beta"}, hops=0)
    assert _paths(kb.search("alp", k=5)) == ["A.md"]
    assert kb.search("alp ", k=5) == []  # once a space follows, it is a word of its own


def test_titles_aliases_and_tags_are_searchable(tmp_path):
    kb = _kb(tmp_path, {
        "Notes/ltr.md": "---\naliases: [Learning to Rank]\ntags:\n  - ranking\n---\nbody",
        "Notes/other.md": "Some #retrieval thoughts.",
    }, hops=0)
    assert _paths(kb.search("learning to rank", k=5)) == ["Notes/ltr.md"]
    assert _paths(kb.search("ranking", k=5)) == ["Notes/ltr.md"]
    assert _paths(kb.search("retrieval", k=5)) == ["Notes/other.md"]


def test_snippet_skips_the_frontmatter_and_centers_on_the_match(tmp_path):
    kb = _kb(tmp_path, {
        "A.md": "---\ntitle: A\ntags: []\n---\nIntro line. The softmax normalizes scores nicely."
    }, hops=0)
    snippet = kb.search("softmax", k=1)[0].snippet
    assert "title: A" not in snippet and "softmax" in snippet


def test_type_filter_skips_the_vault_unless_markdown_is_wanted(tmp_path):
    kb = _kb(tmp_path, {"Note.md": "hello world"}, hops=0)
    assert kb.search("hello", k=3, exts=("pdf",)) == []
    assert _paths(kb.search("hello", k=3, exts=("txt", "md"))) == ["Note.md"]


def test_hidden_folders_and_excluded_files_stay_out(tmp_path):
    root = _vault(tmp_path, {
        "Keep.md": "hello", ".trash/Gone.md": "hello", "Archive/Old.md": "hello",
        "Drafts/x.md": "hello",
    })
    (root / ".obsidian" / "app.json").write_text(
        json.dumps({"userIgnoreFilters": ["Archive/", "/^Drafts/"]}))
    kb = KnowledgeBaseIndex(_cfg(tmp_path, root, hops=0))
    assert _paths(kb.search("hello", k=10)) == ["Keep.md"]


# -- links ----------------------------------------------------------------------------


def test_graph_expansion_follows_links_and_backlinks(tmp_path):
    # The query matches only Attention; one hop brings in the note it links to (Positional
    # Encoding) and the note linking to it (Transformers), neither of which matches.
    notes = {
        "Attention.md": "softmax over scores, see [[Positional Encoding]]",
        "Positional Encoding.md": "sines and cosines",
        "Transformers.md": "built on ![[Attention#Scores|attention]]",
        "Unrelated.md": "nothing",
    }
    paths = set(_paths(_kb(tmp_path, notes, hops=1).search("softmax", k=10)))
    assert paths == {"Attention.md", "Positional Encoding.md", "Transformers.md"}
    assert _paths(KnowledgeBaseIndex(_cfg(tmp_path, tmp_path / "vault", hops=0))
                  .search("softmax", k=10)) == ["Attention.md"]


def test_links_resolve_the_way_obsidian_resolves_them(tmp_path):
    kb = _kb(tmp_path, {
        "Home.md": ("[[Plan]] [[2024/Plan|this year]] [[Alias Name]] [[Topics/sub/Deep]] "
                    "[text](Topics/My%20Note.md) [[photo.png]] [[Not Yet Written]] "
                    "[site](https://example.org/x.md)"),
        "Plan.md": "", "2024/Plan.md": "", "Topics/My Note.md": "",
        "Topics/sub/Deep.md": "", "Aliased.md": "---\naliases:\n  - Alias Name\n---\n",
        "Projects/Plan.md": "[[Plan]]",  # the Plan in its own folder is the one it means
        "Projects/Index.md": "---\nup: \"[[Home]]\"\n---\n",  # a link in a property counts
    })
    kb._refresh(force=True)
    assert kb._links["Home.md"] == {
        "Plan.md", "2024/Plan.md", "Aliased.md", "Topics/sub/Deep.md", "Topics/My Note.md"}
    assert kb._links["Projects/Plan.md"] == set()  # [[Plan]] there is itself
    assert kb._links["Projects/Index.md"] == {"Home.md"}
    assert "Projects/Index.md" in kb._backlinks["Home.md"]


# -- keeping up with the files ----------------------------------------------------------


def test_edits_new_and_deleted_notes_show_up(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_module, "_REFRESH_S", 0.0)
    root = _vault(tmp_path, {"A.md": "alpha", "B.md": "beta"})
    kb = KnowledgeBaseIndex(_cfg(tmp_path, root, hops=0))
    assert _paths(kb.search("alpha", k=5)) == ["A.md"]

    (root / "A.md").write_text("alpha, edited")
    os.utime(root / "A.md", ns=(1, 1))  # a different stamp, however fast the test runs
    (root / "C.md").write_text("alpha too")
    (root / "B.md").unlink()
    hits = kb.search("alpha", k=5)
    assert set(_paths(hits)) == {"A.md", "C.md"}
    assert "edited" in next(h.snippet for h in hits if h.path == "A.md")
    assert kb.search("beta", k=5) == []


def test_the_files_are_looked_at_every_couple_of_seconds_at_most(tmp_path, monkeypatch):
    root = _vault(tmp_path, {"A.md": "alpha"})
    kb = KnowledgeBaseIndex(_cfg(tmp_path, root, hops=0))
    kb.search("alpha", k=5)
    walks = []
    monkeypatch.setattr(kb_module.os, "walk", lambda *a, **k: walks.append(a) or iter(()))
    kb.search("alph", k=5)
    assert walks == []  # search as you type: one look serves the keystrokes that follow


# -- which vault ----------------------------------------------------------------------


def test_the_vault_comes_from_obsidians_own_list(tmp_path, monkeypatch):
    work, home = tmp_path / "Work", tmp_path / "Home"
    work.mkdir()
    home.mkdir()
    registry = tmp_path / "obsidian.json"
    registry.write_text(json.dumps({"vaults": {
        "a": {"path": str(work), "ts": 2},
        "b": {"path": str(home), "ts": 1, "open": True},
    }}))
    monkeypatch.setattr(kb_module, "_registries", lambda: [registry])
    cfg = _cfg(tmp_path)
    assert vault_root(cfg.kb) == home  # the one open in Obsidian
    cfg.kb.vault = "Work"
    assert vault_root(cfg.kb) == work  # the one named
    cfg.kb.vault = "Missing"
    assert vault_root(cfg.kb) is None
    cfg.kb.vault_path = str(work)
    assert vault_root(cfg.kb) == work  # a path given outright wins


def test_no_vault_raises_and_hybrid_skips_kb(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_module, "_registries", list)
    cfg = _cfg(tmp_path)
    with pytest.raises(VaultUnavailable):
        KnowledgeBaseIndex(cfg).search("anything", k=3)
    assert isinstance(VaultUnavailable("x"), FileNotFoundError)
    out = SearchEngine(cfg).search_hybrid("x", k=3, retrievers=["kb"])
    assert out.used == [] and "kb" in out.skipped


# -- in the engine --------------------------------------------------------------------


def test_hybrid_includes_kb_and_results_point_at_the_note_file(tmp_path):
    root = _vault(tmp_path, {"Note.md": "hello world"})
    engine = SearchEngine(_cfg(tmp_path, root))
    out = engine.search_hybrid("hello", k=3)
    assert out.used == ["kb"]
    assert out.results and out.results[0].retriever == "hybrid"
    hit = engine.search_kb("hello", k=1)[0]
    assert hit.abs_path == str(root / "Note.md") and hit.mtime is not None


def test_hybrid_exclude_never_reads_the_vault(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, _vault(tmp_path, {"Note.md": "hello world"}))
    cfg.graph.enabled = True
    engine = SearchEngine(cfg)
    monkeypatch.setattr(kb_module.os, "walk", lambda *a, **k: pytest.fail("vault read"))
    out = engine.search("hybrid", "hello", k=3, exclude=("kb",))
    assert out.used == ["graph"]
    assert out.skipped == {}  # left out on request, not a backend that failed


# -- writing --------------------------------------------------------------------------


def test_notes_are_written_only_inside_the_agent_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_module, "_REFRESH_S", 0.0)
    root = _vault(tmp_path, {"Mine.md": "my own note"})
    kb = KnowledgeBaseIndex(_cfg(tmp_path, root, hops=0))
    kb.create_note("Agent Knowledge/Topics/x.md", "line one\nline two")
    assert (root / "Agent Knowledge/Topics/x.md").read_text() == "line one\nline two"
    assert kb.read_note("Agent Knowledge/Topics/x.md") == "line one\nline two"
    assert kb.read_note("Agent Knowledge/missing.md") is None
    with pytest.raises(FileExistsError):
        kb.create_note("Agent Knowledge/Topics/x.md", "again")
    kb.create_note("Agent Knowledge/Topics/x.md", "replaced", overwrite=True)
    kb.append_note("Agent Knowledge/Topics/x.md", "- one more")
    assert kb.read_note("Agent Knowledge/Topics/x.md") == "replaced\n- one more\n"
    assert _paths(kb.search("replaced", k=3)) == ["Agent Knowledge/Topics/x.md"]

    for elsewhere in ("Mine.md", "Agent Knowledge/../Mine.md", "/etc/x.md", "Agent Knowledgeable/x.md"):
        with pytest.raises(ValueError):
            kb.create_note(elsewhere, "overwritten")
    assert (root / "Mine.md").read_text() == "my own note"
    assert list(root.rglob(".infogrep-*")) == []  # no half-written files left about


def test_html_in_imported_notes_is_read_past(tmp_path):
    kb = _kb(tmp_path, {
        "Evernote.md": ('<div><span style="font-family:PingFangSC">领取护照</span>&nbsp;'
                        '<br/>北京&amp;清华</div><style>p { color: red }</style>'),
        "Plain.md": "a note about fonts and span widths",
    }, hops=0)
    snippet = kb.search("领取护照", k=1)[0].snippet
    assert snippet == "领取护照 北京&清华"
    assert _paths(kb.search("span", k=5)) == ["Plain.md"]  # markup is not a word of the note
    assert kb.search("color", k=5) == []


def test_warming_reads_the_vault(tmp_path):
    root = _vault(tmp_path, {"A.md": "alpha"})
    engine = SearchEngine(_cfg(tmp_path, root))
    engine.warm()
    assert list(engine.kb._notes) == ["A.md"]
