import pytest

from infogrep.config import Config
from infogrep.engine import SearchEngine
from infogrep.retrieval.kb import KnowledgeBaseIndex, _link_target_key


def _vault(root):
    """A tiny linked vault: 'Transformers' links to 'Attention', which mentions softmax."""
    (root / "Transformers.md").write_text(
        "# Transformers\n\nThe transformer architecture relies on [[Attention]].\n#nlp"
    )
    (root / "Attention.md").write_text(
        "# Attention\n\nScaled dot-product attention uses a softmax over scores.\n"
        "See also [[Positional Encoding]]."
    )
    (root / "Positional Encoding.md").write_text(
        "# Positional Encoding\n\nSinusoidal position signals added to embeddings."
    )
    (root / "Bananas.md").write_text("# Bananas\n\nRich in potassium. Unrelated note.")


def _cfg(tmp_path, vault):
    cfg = Config.load(tmp_path)
    cfg.kb.enabled = True
    cfg.kb.vault_path = str(vault)
    cfg.sparse.enabled = False
    cfg.dense.enabled = False
    return cfg


def test_link_target_key_normalization():
    assert _link_target_key("Attention") == "attention"
    assert _link_target_key("Attention|attn") == "attention"
    assert _link_target_key("Attention#Scaled") == "attention"
    assert _link_target_key("folder/Attention") == "attention"


def test_text_match_finds_note(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _vault(vault)
    kb = KnowledgeBaseIndex(_cfg(tmp_path, vault))
    hits = kb.search("scaled dot-product softmax", k=5)
    assert hits[0].path == "Attention.md"
    assert hits[0].retriever == "kb"


def test_graph_expansion_surfaces_linked_note(tmp_path):
    # Query matches only 'Attention'; with hops>=1, the linking note 'Transformers'
    # (which does NOT contain 'softmax') should still surface via the link graph.
    vault = tmp_path / "vault"
    vault.mkdir()
    _vault(vault)
    cfg = _cfg(tmp_path, vault)

    cfg.kb.hops = 0
    no_hop = {h.path for h in KnowledgeBaseIndex(cfg).search("softmax scores", k=10)}
    assert "Transformers.md" not in no_hop  # only direct text matches

    cfg.kb.hops = 1
    with_hop = {h.path for h in KnowledgeBaseIndex(cfg).search("softmax scores", k=10)}
    assert "Attention.md" in with_hop
    assert "Transformers.md" in with_hop  # pulled in by [[Attention]] backlink expansion


def test_tag_query(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _vault(vault)
    kb = KnowledgeBaseIndex(_cfg(tmp_path, vault))
    hits = kb.search("#nlp", k=5)
    assert any(h.path == "Transformers.md" for h in hits)


def test_missing_vault_raises(tmp_path):
    cfg = Config.load(tmp_path)
    cfg.kb.enabled = True
    cfg.kb.vault_path = str(tmp_path / "does-not-exist")
    with pytest.raises(FileNotFoundError):
        KnowledgeBaseIndex(cfg).search("anything", k=3)


def test_hybrid_includes_kb_when_enabled(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _vault(vault)
    cfg = _cfg(tmp_path, vault)  # only kb enabled
    out = SearchEngine(cfg).search_hybrid("attention softmax", k=5)
    assert out.used == ["kb"]
    assert out.results and out.results[0].retriever == "hybrid"
