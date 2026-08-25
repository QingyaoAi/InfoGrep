"""Build a query-centric knowledge graph in the Obsidian vault from indexed files.

``infogrep learn <query>`` searches the indexed directory (sparse/dense/graph — never
kb itself, so the builder can't learn from its own output), extracts salient entities
from the retrieved passages, and materializes the findings as interlinked notes inside
the vault's agent folder (``kb.agent_folder``, default "Agent Knowledge"):

    <agent_folder>/Topics/<query>.md      hub note: source paths + snippets + entity links
    <agent_folder>/Entities/<entity>.md   one note per entity, linking back to its topics

Because the notes are joined by ``[[wikilinks]]``, a later ``search_kb`` (or hybrid)
query that hits any of them expands along links/backlinks and surfaces the whole
neighborhood — topic, entities, and source citations — without re-searching the files.

Entity extraction is deliberately model-free (capitalized-phrase and acronym mining
over the retrieved snippets, ranked by cross-document frequency): cheap, deterministic,
and local, in keeping with the rest of InfoGrep.
"""

from __future__ import annotations

import itertools
import re
import time
from collections import Counter, defaultdict

from .retrieval.base import Result

# Capitalized phrase or acronym; the working unit of entity mining. Only lowercase
# connectors that are genuinely part of names may appear inside ("Chain of Thought",
# "University of Washington") — coordinators like "and" would merge distinct entities.
_PHRASE_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9'’&-]*"
    r"(?:[ ](?:of|for|de|la|van|von|[A-Z][A-Za-z0-9'’&-]*)){0,4}\b"
)
# CJK has no capitalization to mine, so entity candidates are short runs of CJK
# characters delimited by non-CJK text/punctuation (names, orgs, terms — e.g. a form
# field's value); longer runs are prose, not terms. Whitespace between CJK characters
# is merged away first: Chinese doesn't use spaces, so it's a PDF line-wrap artifact
# that would otherwise shred names into fragments (北京搜狗科技发 + 展有限公司).
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
_CJK_SPACE_RE = re.compile(r"(?<=[一-鿿])\s+(?=[一-鿿])")
_MAX_CJK_ENTITY_CHARS = 12
# Runs of 7-12 chars are usually prose clauses; admit them only when they end like an
# organization name (北京搜狗科技发展有限公司, 中国计算机学会).
_CJK_ORG_SUFFIXES = ("公司", "大学", "学院", "研究所", "研究院", "实验室", "中心", "集团", "委员会", "学会")
_MAX_CJK_SHORT_CHARS = 6
# Characters Obsidian rejects (or treats specially) in note names.
_UNSAFE_TITLE_RE = re.compile(r"[*\"\\/<>:|?#^\[\]]+")

_MAX_TITLE_CHARS = 80
_MAX_ENTITY_CHARS = 60
_MAX_SNIPPET_CHARS = 280
_MAX_SNIPPETS_PER_FILE = 2

# Sentence-starters, discourse words, and document boilerplate — safe to strip off
# phrase *edges* ("ABSTRACT Legal" -> "Legal") and never entities on their own.
_TRIM_WORDS = frozenset(
    """
    a an and are as at be but by for from he her his how however i if in is it its
    no not of on or our she so that the their then there these they this those thus
    to was we were what when where which while who why with you your also during see
    figure table section page chapter abstract introduction conclusion conclusions
    references appendix results method methods discussion related work summary
    anonymous anon author authors et al woodstock ny
    january february march april may june july august september october november
    december monday tuesday wednesday thursday friday saturday sunday
    """.split()
)
# Form-field labels ("Given Name", "证件类型"): virtually never part of a real entity
# name, so they are also stripped off phrase edges ("Given Name Liu Yiqun" -> "Liu
# Yiqun") — unlike _GENERIC_WORDS below, which can open a legitimate name.
_LABEL_WORDS = frozenset(
    """
    name surname given gender male female nationality id number date birth identity
    card contact personal address email phone tel city province passport title
    patent primary contributor role applicant accomplishments grant year award
    姓名 性别 国籍 出生日期 身份证 证件 证件号 证件类型 类型 电话 邮箱 地址 拼音
    个人信息 基础信息 联系方式 申请人 专利 专利权人 发明人 发明 授权 日期 国家 证书 编号
    """.split()
)
# Generic nouns: never distinctive enough to be entities alone, but NOT edge-trimmed —
# "Information Retrieval" must survive intact. A candidate needs at least one token
# outside all of these lists.
_GENERIC_WORDS = (
    frozenset(
        """
        paper papers research project projects proposal proposals result study model
        approach example information china ai country beijing 中国 北京
        """.split()
    )
    | _LABEL_WORDS
)
_EDGE_TRIM_WORDS = _TRIM_WORDS | _LABEL_WORDS
_NONENTITY_WORDS = _TRIM_WORDS | _GENERIC_WORDS


def sanitize_title(text: str) -> str:
    """Make ``text`` safe to use as an Obsidian note name."""
    clean = " ".join(_UNSAFE_TITLE_RE.sub(" ", text).split()).strip(". ")
    return clean[:_MAX_TITLE_CHARS].strip()


def _trim_phrase(phrase: str, drop: set[str]) -> str:
    """Strip stopword/query tokens off both edges ("ABSTRACT Legal" -> "Legal")."""
    words = phrase.split()
    while words and words[0].lower() in drop:
        words.pop(0)
    while words and words[-1].lower() in drop:
        words.pop()
    return " ".join(words)


def _is_acronym(word: str) -> bool:
    return word.isupper() and 2 <= len(word) <= 6


def _looks_like_identifier(word: str) -> bool:
    """IDs, not names: digits mixed into lowercase ("UKqaI5IAAAAJ"; acronym-with-digits
    like BM25 stays), 4+ consecutive digits (patent/serial numbers like ZL202310369775,
    even truncated to ZL2013), or repeated internal case flips beyond CamelCase."""
    if any(c.isdigit() for c in word) and any(c.islower() for c in word):
        return True
    if re.search(r"\d{4}", word):
        return True
    return sum(1 for a, b in itertools.pairwise(word) if a.islower() and b.isupper()) >= 2


def extract_entities(
    results: list[Result], query: str, max_entities: int
) -> dict[str, set[str]]:
    """Mine salient capitalized phrases/acronyms and short CJK runs from snippets.

    Returns ``{entity: {source paths it was seen in}}``, best-first insertion order.
    Single tokens are noisy (sentence starts; CJK runs have no capitalization signal
    at all), so they must either be acronyms or recur across documents; multi-word
    phrases pass on one sighting.
    """
    query_tokens = {t.lower() for t in re.findall(r"\w+", query)}
    counts: Counter[str] = Counter()
    docs: dict[str, set[str]] = defaultdict(set)
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)

    def consider(phrase: str, path: str, n: int = 1) -> None:
        tokens = [t.lower() for t in phrase.split()]
        content_tokens = {t for t in tokens if t not in _NONENTITY_WORDS}
        # No distinctive token ("Contact Information"), or all content words already
        # in the query -> the topic itself, not a related entity.
        if not content_tokens or content_tokens <= query_tokens:
            return
        if any(_looks_like_identifier(w) for w in phrase.split()):
            return
        key = phrase.lower()
        counts[key] += n
        docs[key].add(path)
        surfaces[key][phrase] += n

    # Mine only real content: a snippet whose every token comes from the file's own
    # path is path text (images, filename-only matches, graph folder notes), and
    # path components make poor entities ("Project Academic CCIR").
    def is_path_echo(result: Result) -> bool:
        snippet_tokens = set(re.findall(r"\w+", (result.snippet or "").lower()))
        path_tokens = set(re.findall(r"\w+", result.path.lower()))
        return bool(snippet_tokens) and snippet_tokens <= path_tokens

    texts = [
        (" ".join((r.snippet or "").split()), r.path) for r in results if not is_path_echo(r)
    ]

    for text, path in texts:
        for m in _PHRASE_RE.finditer(text):
            if m.end() == len(text):  # snippet truncation point: likely a cut word
                continue
            phrase = _trim_phrase(m.group(), _EDGE_TRIM_WORDS)
            if phrase and len(phrase) <= _MAX_ENTITY_CHARS:
                consider(phrase, path)

    # CJK: standalone runs (form values, titles, list items) seed a lexicon, whose
    # terms are then counted as substrings everywhere — prose has no word delimiters,
    # so a term seen standalone once is also credited where it sits inside another
    # document's sentence.
    def cjk_candidate(run: str) -> bool:
        if not 2 <= len(run) <= _MAX_CJK_ENTITY_CHARS:
            return False
        return len(run) <= _MAX_CJK_SHORT_CHARS or run.endswith(_CJK_ORG_SUFFIXES)

    cjk_texts = [(_CJK_SPACE_RE.sub("", text), path) for text, path in texts]
    lexicon = {
        m.group()
        for text, _ in cjk_texts
        for m in _CJK_RUN_RE.finditer(text)
        if cjk_candidate(m.group()) and m.end() != len(text)
    }
    for text, path in cjk_texts:
        for term in lexicon:
            n = text.count(term)
            if n:
                consider(term, path, n)

    # Nested CJK n-grams: credit the longer term (清华大学 over 清华) unless the
    # shorter one also occurs on its own.
    for key in [k for k in counts if _CJK_RUN_RE.fullmatch(k)]:
        within = sum(counts[other] for other in counts if other != key and key in other)
        if within and counts[key] <= within:
            del counts[key]

    # Merge Latin variants whose distinctive words are identical ("Project Yiqun Liu",
    # "Yiqun Liu", "Liu Yiqun"): the fewest-token spelling is the cleanest name.
    canon_by_content: dict[frozenset[str], str] = {}
    for key in sorted(counts, key=lambda k: (len(k.split()), len(k))):
        if _CJK_RUN_RE.search(key):
            continue
        content = frozenset(t for t in key.split() if t not in _NONENTITY_WORDS)
        canon = canon_by_content.setdefault(content, key)
        if canon != key:
            counts[canon] += counts.pop(key)
            docs[canon] |= docs.pop(key)

    def eligible(key: str) -> bool:
        single_word = " " not in key
        if single_word and not _is_acronym(surfaces[key].most_common(1)[0][0]):
            return len(docs[key]) >= 2
        return True

    ranked = sorted(
        (key for key in counts if eligible(key)),
        key=lambda key: (-(2 * len(docs[key]) + counts[key]), key),
    )
    out: dict[str, set[str]] = {}
    for key in ranked[:max_entities]:
        name = sanitize_title(surfaces[key].most_common(1)[0][0])
        if name:
            out[name] = docs[key]
    return out


class KnowledgeGraphBuilder:
    """Turn one query's search results into interlinked vault notes."""

    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.kb = engine.kb  # KnowledgeBaseIndex: CLI plumbing + note read/write
        self.folder = self.config.kb.agent_folder.strip("/")

    # -- note paths / links --------------------------------------------------

    def _topic_path(self, title: str) -> str:
        return f"{self.folder}/Topics/{title}.md"

    def _entity_path(self, name: str) -> str:
        return f"{self.folder}/Entities/{name}.md"

    @staticmethod
    def _link(path: str, label: str) -> str:
        return f"[[{path.removesuffix('.md')}|{label}]]"

    # -- note bodies -----------------------------------------------------------

    def _topic_note(
        self, query: str, title: str, results: list[Result], entities: dict[str, set[str]]
    ) -> str:
        lines = [
            "---",
            "tags:",
            "  - infogrep/topic",
            f'query: "{query.replace(chr(34), chr(39))}"',
            f"built: {time.strftime('%Y-%m-%d')}",
            f"directory: {self.config.target_dir}",
            "---",
            "",
            f"# {title}",
            "",
            (
                "Auto-built by `infogrep learn` from files indexed under "
                f"`{self.config.target_dir}`. Rebuilding overwrites this note."
            ),
            "",
        ]
        if entities:
            lines += ["## Related entities", ""]
            lines += [f"- {self._link(self._entity_path(e), e)}" for e in entities]
            lines.append("")
        lines += ["## Sources", ""]
        per_file: dict[str, list[Result]] = defaultdict(list)
        for r in results:
            per_file[r.path].append(r)
        for path, hits in per_file.items():
            best = hits[0]
            where = f" p.{best.page}" if best.page is not None else ""
            lines.append(f"- `{path}`{where} ({best.retriever}, score {best.score:.3f})")
            for hit in hits[:_MAX_SNIPPETS_PER_FILE]:
                snippet = " ".join((hit.snippet or "").split())[:_MAX_SNIPPET_CHARS]
                if snippet:
                    lines.append(f"  > {snippet}")
        lines.append("")
        return "\n".join(lines)

    def _entity_header(self, name: str) -> str:
        return "\n".join(
            ["---", "tags:", "  - infogrep/entity", "---", "", f"# {name}", "", "## Mentions", ""]
        )

    def _mention_line(self, title: str, sources: set[str]) -> str:
        cited = ", ".join(f"`{p}`" for p in sorted(sources)[:5])
        link = self._link(self._topic_path(title), title)
        return f"- {link} ({time.strftime('%Y-%m-%d')}) — seen in {cited}"

    # -- build -----------------------------------------------------------------

    def build(self, query: str, k: int = 12, max_entities: int = 8) -> dict:
        """Search indexed files for ``query`` and write the topic + entity notes.

        Idempotent: the topic note is regenerated in place, and an entity note only
        gains a mention line the first time it's linked to a given topic.
        """
        title = sanitize_title(query)
        if not title:
            raise ValueError("query is empty after sanitization")

        # Learn from content retrievers only; kb itself is the output, not an input.
        # Graph hits join the Sources section (co-located files are good citations)
        # but are excluded from entity mining — their "snippets" are just path text.
        out = self.engine.search_hybrid(query, k=k, retrievers=["sparse", "dense"])
        graph_hits: list[Result] = []
        if self.config.graph.enabled:
            try:
                graph_hits = self.engine.search_graph(query, k=k)
            except (FileNotFoundError, RuntimeError):
                pass  # no graph index yet — content results alone are fine
        content_paths = {r.path for r in out.results}
        extras = [r for r in graph_hits if r.path not in content_paths]
        sources = out.results + extras

        summary = {
            "query": query,
            "topic_note": None,
            "entities": [],
            "entity_notes_created": [],
            "entity_notes_updated": [],
            "entity_notes_unchanged": [],
            "n_sources": 0,
            "n_passages": len(sources),
            "retrievers_used": out.used + (["graph"] if graph_hits else []),
            "retrievers_skipped": out.skipped,
            "kb_search_enabled": self.config.kb.enabled,
        }
        if not sources:
            return summary  # nothing found -> don't write an empty note

        entities = extract_entities(out.results, query, max_entities)
        topic_path = self._topic_path(title)
        self.kb.create_note(
            topic_path, self._topic_note(query, title, sources, entities), overwrite=True
        )
        summary["topic_note"] = topic_path
        summary["entities"] = list(entities)
        summary["n_sources"] = len({r.path for r in sources})

        topic_link_marker = f"[[{topic_path.removesuffix('.md')}"
        for name, sources in entities.items():
            path = self._entity_path(name)
            existing = self.kb.read_note(path)
            if existing is None:
                self.kb.create_note(
                    path, self._entity_header(name) + self._mention_line(title, sources) + "\n"
                )
                summary["entity_notes_created"].append(path)
            elif topic_link_marker in existing:
                summary["entity_notes_unchanged"].append(path)
            else:
                self.kb.append_note(path, self._mention_line(title, sources))
                summary["entity_notes_updated"].append(path)
        return summary
