"""Tests for ``infogrep learn`` (kb_builder): entity mining + vault note writing."""

from infogrep.config import Config
from infogrep.engine import HybridResults, SearchEngine
from infogrep.kb_builder import KnowledgeGraphBuilder, extract_entities, sanitize_title
from infogrep.retrieval.base import Result
from infogrep.retrieval.kb import KnowledgeBaseIndex


def _unescape(text: str) -> str:
    """Inverse of the CLI's content escaping (\\\\, \\n, \\t)."""
    return (
        text.replace("\\\\", "\x00").replace("\\n", "\n").replace("\\t", "\t").replace("\x00", "\\")
    )


class FakeVault:
    """Writable stand-in for the Obsidian CLI, mimicking its quirks.

    Notably: ``create`` without ``overwrite`` on an existing path silently writes
    to ``<name> 1.md`` (as the real CLI does) instead of failing.
    """

    def __init__(self):
        self.notes: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, command, params):
        self.calls.append((command, params))
        path = params.get("path", "")
        if command == "read":
            return self.notes.get(path) or "Error: file not found"
        if command == "create":
            if path in self.notes and "overwrite" not in params:
                path = f"{path.removesuffix('.md')} 1.md"
            self.notes[path] = _unescape(params["content"])
            return f"Created: {path}"
        if command == "append":
            self.notes[path] = self.notes.get(path, "") + "\n" + _unescape(params["content"])
            return f"Appended to {path}"
        return ""


def _res(path, snippet, score=1.0, retriever="sparse", page=None):
    return Result(
        doc_id=path, passage_id=f"{path}#0", path=path, snippet=snippet,
        score=score, retriever=retriever, page=page,
    )


def _builder(tmp_path, results, graph_results=None):
    """Engine with a fake vault and stubbed searches returning canned results."""
    cfg = Config.load(tmp_path)
    engine = SearchEngine(cfg)
    fake = FakeVault()
    engine._backends["kb"] = KnowledgeBaseIndex(cfg, runner=fake)
    engine.search_hybrid = lambda query, k=10, retrievers=None, prf=False: HybridResults(
        results=results, used=["sparse"]
    )
    engine.search_graph = lambda query, k=10: graph_results or []
    return KnowledgeGraphBuilder(engine), fake


# -- entity extraction -------------------------------------------------------


def test_extract_multiword_phrases_and_sources():
    results = [
        _res("a.pdf", "We compare against Dense Passage Retrieval on legal corpora."),
        _res("b.pdf", "Dense Passage Retrieval outperforms BM25 here. BM25 remains strong."),
    ]
    entities = extract_entities(results, "legal case retrieval", max_entities=8)
    assert "Dense Passage Retrieval" in entities
    assert entities["Dense Passage Retrieval"] == {"a.pdf", "b.pdf"}
    assert "BM25" in entities  # acronym: allowed from a single document


def test_extract_filters_query_stopwords_and_lone_words():
    results = [
        _res("a.md", "The Attention mechanism. However, results vary."),
        _res("b.md", "Retrieval quality improves."),
    ]
    # "Retrieval" is a query term; "However"/"The" are stopwords; "Attention" is a
    # single non-acronym word seen in only one document.
    assert extract_entities(results, "retrieval", max_entities=8) == {}
    # ...but a single word recurring across documents qualifies.
    both = [_res("a.md", "Transformer layers."), _res("b.md", "a Transformer variant.")]
    assert "Transformer" in extract_entities(both, "layers", max_entities=8)


def test_extract_filters_identifiers_and_boilerplate():
    results = [
        _res("cv.pdf", "Scholar profile UKqaI5IAAAAJ. ABSTRACT Legal issues. Anonymous Author."),
        _res("cv2.pdf", "UKqaI5IAAAAJ again. Result Diversification for Legal cases."),
    ]
    entities = extract_entities(results, "legal case diversification", max_entities=8)
    # Random IDs, section headers, review boilerplate, and query-only phrases all drop.
    assert entities == {}
    # ...while CamelCase and acronyms-with-digits survive the identifier filter.
    ok = [_res("a.md", "PageRank vs BM25."), _res("b.md", "PageRank and BM25 again.")]
    got = extract_entities(ok, "ranking", max_entities=8)
    assert {"PageRank", "BM25"} <= set(got)


def test_extract_filters_form_labels_but_keeps_domain_terms():
    results = [
        _res("form.pdf", "姓名 / Name 刘奕群 * 性别 / Gender 男 | Male * 证件号 / ID Number"),
        _res("form2.pdf", "Personal Information. Contact Information. Information Retrieval Lab."),
    ]
    entities = extract_entities(results, "刘奕群", max_entities=8)
    for label in ("Name", "Gender", "Male", "ID Number", "Personal Information", "姓名", "性别"):
        assert label not in entities
    # "information" is a generic token, but it must not be trimmed off a real term.
    assert "Information Retrieval Lab" in entities


def test_extract_trims_label_prefixes_and_cut_words():
    results = [
        _res("form.pdf", "名（拼音）/ Given Name Liu Yiqun * 出生日期 / Date of Bir"),
        _res("form2.pdf", "Surname Liu Yiqun, Beijing. Patent Title * Date of B"),
    ]
    entities = extract_entities(results, "刘奕群", max_entities=8)
    # Label prefixes come off the value; words cut at the snippet edge never count.
    assert "Liu Yiqun" in entities
    for junk in ("Given Name Liu Yiqun", "Date of B", "Date of Bir", "Patent Title"):
        assert junk not in entities


def test_extract_cjk_runs():
    results = [
        _res("a.md", "刘奕群教授就职于清华大学。研究方向为信息检索。"),
        _res("b.md", "清华大学、信息检索：课题组会议记录。"),
    ]
    entities = extract_entities(results, "刘奕群", max_entities=8)
    # Short CJK runs recurring across documents qualify; the query itself does not,
    # and long prose runs (刘奕群教授就职于清华大学) are not split into terms.
    assert "清华大学" in entities and "信息检索" in entities
    assert "刘奕群" not in entities


def test_extract_cjk_merges_line_wrapped_org_names():
    # PDF extraction wraps 北京搜狗科技发展有限公司 mid-name; the space is an artifact,
    # and the reassembled name qualifies as a long run via its 公司 suffix.
    results = [
        _res("patent1.pdf", "专利权人：北京搜狗科技发 展有限公司。发明人：刘奕群。"),
        _res("patent2.pdf", "专利权人：北京搜狗科技发 展有限公司。授权日期：某日。"),
    ]
    entities = extract_entities(results, "刘奕群", max_entities=8)
    assert "北京搜狗科技发展有限公司" in entities
    # No shredded fragments, and patent-table labels stay out.
    for junk in ("展有限公司", "京搜狗科技发", "专利权人", "发明人", "授权", "日期"):
        assert junk not in entities


def test_extract_merges_latin_variants_differing_by_generic_words():
    results = [
        _res("a.pdf", "Contact Yiqun Liu directly."),
        _res("b.pdf", "See the folder Project Yiqun Liu for details."),
    ]
    entities = extract_entities(results, "刘奕群", max_entities=8)
    assert "Yiqun Liu" in entities
    assert "Project Yiqun Liu" not in entities
    assert entities["Yiqun Liu"] == {"a.pdf", "b.pdf"}


def test_sanitize_title_strips_obsidian_unsafe_chars():
    assert sanitize_title('what is "RAG": a [survey]?') == "what is RAG a survey"
    assert sanitize_title("a/b\\c") == "a b c"


# -- build ---------------------------------------------------------------------


def test_build_writes_linked_topic_and_entity_notes(tmp_path):
    results = [
        _res("papers/dpr.pdf", "Dense Passage Retrieval beats BM25.", score=2.0, page=3),
        _res("notes/eval.md", "Dense Passage Retrieval, evaluated with BM25 baselines."),
    ]
    builder, vault = _builder(tmp_path, results)
    out = builder.build("legal search", k=5)

    topic_path = "Agent Knowledge/Topics/legal search.md"
    assert out["topic_note"] == topic_path
    assert out["n_sources"] == 2
    topic = vault.notes[topic_path]
    # Sources pair every snippet with its file path; page numbers survive.
    assert "`papers/dpr.pdf` p.3" in topic and "Dense Passage Retrieval beats BM25." in topic
    # Topic links to entities; entity notes link back (both full-path wikilinks).
    entity_path = "Agent Knowledge/Entities/Dense Passage Retrieval.md"
    assert "[[Agent Knowledge/Entities/Dense Passage Retrieval|" in topic
    assert entity_path in out["entity_notes_created"]
    assert "[[Agent Knowledge/Topics/legal search|legal search]]" in vault.notes[entity_path]
    assert "`papers/dpr.pdf`" in vault.notes[entity_path]


def test_rebuild_is_idempotent(tmp_path):
    results = [
        _res("a.pdf", "Chain of Thought prompting."),
        _res("b.pdf", "Chain of Thought results."),
    ]
    builder, vault = _builder(tmp_path, results)
    first = builder.build("prompting tricks")
    again = builder.build("prompting tricks")

    entity_path = first["entity_notes_created"][0]
    assert again["entity_notes_unchanged"] == [entity_path]
    assert again["entity_notes_created"] == []
    # Exactly one mention line despite two builds, and no "name 1.md" duplicates.
    assert vault.notes[entity_path].count("[[Agent Knowledge/Topics/prompting tricks|") == 1
    assert not [p for p in vault.notes if p.endswith(" 1.md")]


def test_existing_entity_note_gains_mention_for_new_topic(tmp_path):
    results = [_res("a.pdf", "Chain of Thought."), _res("b.pdf", "Chain of Thought.")]
    builder, vault = _builder(tmp_path, results)
    entity_path = "Agent Knowledge/Entities/Chain of Thought.md"
    vault.notes[entity_path] = "# Chain of Thought\n\n## Mentions\n"

    out = builder.build("reasoning")
    assert out["entity_notes_updated"] == [entity_path]
    assert "[[Agent Knowledge/Topics/reasoning|reasoning]]" in vault.notes[entity_path]


def test_graph_hits_are_sources_but_not_mined_for_entities(tmp_path):
    content = [
        _res("papers/a.pdf", "Query Performance Prediction results."),
        _res("papers/b.pdf", "Query Performance Prediction, again."),
    ]
    graph = [
        _res(
            "assets/photo.png",
            'co-located in folder "Project Academic CCIR Meeting" (metadata graph match)',
            retriever="graph",
        )
    ]
    builder, vault = _builder(tmp_path, content, graph_results=graph)
    out = builder.build("prediction methods")
    topic = vault.notes[out["topic_note"]]
    assert "`assets/photo.png`" in topic  # cited as a source...
    assert "Project Academic CCIR" not in out["entities"]  # ...but path text isn't mined
    assert out["n_sources"] == 3
    assert "graph" in out["retrievers_used"]


def test_extract_skips_snippets_that_only_echo_the_path():
    # Images and filename-only matches have path text as their "content".
    results = [
        _res("Project/Academic/CCIR/assets/刘奕群.png", "Project Academic CCIR assets 刘奕群 png"),
        _res("Project/Academic/CCIR/assets/photo2.png", "Project Academic CCIR assets photo2 png"),
    ]
    assert extract_entities(results, "刘奕群", max_entities=8) == {}


def test_extract_rejects_serial_numbers():
    results = [
        _res("a.pdf", "Grant ZL202310369775 issued, prefix ZL2013 shown. See RankNet model."),
        _res("b.pdf", "Grant ZL202310369775 again, ZL2013 and RankNet too."),
    ]
    entities = extract_entities(results, "patents", max_entities=8)
    assert "RankNet" in entities
    assert not any("ZL" in e for e in entities)


def test_no_results_writes_nothing(tmp_path):
    builder, vault = _builder(tmp_path, [])
    out = builder.build("anything")
    assert out["topic_note"] is None
    assert vault.notes == {}


def test_create_note_escapes_newlines_for_cli(tmp_path):
    builder, vault = _builder(tmp_path, [_res("a.pdf", "X Y"), _res("b.pdf", "X Y")])
    builder.build("q")
    create_calls = [p for c, p in vault.calls if c == "create"]
    assert create_calls and all("\n" not in p["content"] for p in create_calls)
    assert any("\\n" in p["content"] for p in create_calls)


def test_engine_learn_entry_point(tmp_path):
    cfg = Config.load(tmp_path)
    engine = SearchEngine(cfg)
    engine._backends["kb"] = KnowledgeBaseIndex(cfg, runner=FakeVault())
    engine.search_hybrid = lambda query, k=10, retrievers=None, prf=False: HybridResults(
        results=[_res("a.md", "Graph RAG pipelines."), _res("b.md", "Graph RAG index.")],
        used=["sparse"],
    )
    out = engine.learn("rag variants")
    assert out["topic_note"] == "Agent Knowledge/Topics/rag variants.md"
    assert "Graph RAG" in out["entities"]
