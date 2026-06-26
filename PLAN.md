# InfoGrep — Development Plan

A single, local-first tool that lets coding agents (Claude Code, Codex) search the
**actual content** of every file in a directory via sparse, dense, and knowledge-base
retrieval — separately or combined.

---

## 1. Design decisions (locked)

| Concern | Choice | Rationale |
|---|---|---|
| Agent interface | **MCP server** | Native structured tool-calling for Claude Code & Codex |
| Sparse retrieval | **Pyserini** (Lucene/BM25) | Mature, robust incremental indexing, battle-tested |
| Dense retrieval | **Zvec** vector DB + **pluggable embedder** | Default `Qwen3-Embedding-0.6B`, swappable from day one |
| Knowledge base | **Obsidian CLI** graph | Link/graph-aware search over a vault |
| Runtime | **Python 3.10+, `uv`** managed, runs on macOS (MPS) | Matches local tooling already installed |
| Resource model | **Disk-backed indices, lazy loading, batch/streaming** | Keep CPU & RAM low; never load all content into memory |

### Guiding constraints (from `thoughts.md`)
- Index in a **side-car location**; never modify original files or directory structure.
- **Incremental** re-indexing: detect changed files (mtime + size + content hash) on a daily cadence.
- **Passage-level** indexing: split long docs into chunks for both sparse and dense.
- Agents can call each retriever **independently or fused**.
- Support **short and long queries**; optional **PRF / query expansion**.

---

## 2. High-level architecture

```
                ┌─────────────────────────────────────────────┐
                │              MCP Server (tools)              │
                │  search_sparse · search_dense · search_kb    │
                │  search_hybrid · index_status · reindex      │
                └───────────────┬──────────────────────────────┘
                                │
          ┌─────────────────────┼─────────────────────────┐
          │                     │                         │
   ┌──────▼──────┐       ┌──────▼──────┐           ┌──────▼──────┐
   │  Sparse     │       │   Dense     │           │ Knowledge   │
   │ (Pyserini)  │       │ (Zvec +     │           │ Base        │
   │  BM25       │       │  embedder)  │           │ (Obsidian)  │
   └──────┬──────┘       └──────┬──────┘           └──────┬──────┘
          └──────────────┬──────┴──────────┬──────────────┘
                         │                 │
                  ┌──────▼──────┐   ┌──────▼───────┐
                  │  Fusion /   │   │  Index store │
                  │  reranker   │   │  (side-car)  │
                  └─────────────┘   └──────┬───────┘
                                           │
                ┌──────────────────────────▼─────────────────────────┐
                │  Ingestion pipeline                                 │
                │  walk → extract (pdf/docx/pptx/…) → chunk → embed   │
                │       → write sparse + dense indices + manifest     │
                └────────────────────────────────────────────────────┘
```

### Side-car layout (per indexed directory)
```
<target_dir>/.infogrep/
├── config.toml            # which retrievers enabled, model name, chunking params
├── manifest.sqlite        # file→hash/mtime/size, doc_id→passages, index versions
├── sparse/                # Pyserini Lucene index
├── dense/                 # Zvec vector store
└── cache/                 # extracted text, embeddings cache
```
`.infogrep/` is the only thing written; originals are read-only.

---

## 3. Core components

### 3.1 Ingestion / extraction
- **File walker** with include/exclude globs and `.gitignore`-style rules.
- **Extractors** keyed by type (registry pattern, easy to extend):
  - PDF → `pymupdf` (fast, no Java), fallback OCR via `ocrmypdf`/`tesseract` for scans.
  - DOCX → `python-docx`; PPTX → `python-pptx`; XLSX/CSV → `openpyxl`/`pandas`.
  - HTML/MD/TXT/code → direct text; EPUB → `ebooklib`.
  - Unknown → `textract`/`tika` fallback (Tika optional to avoid JVM bloat).
- **Chunker**: sentence/recursive splitter, configurable `chunk_size` + `overlap`,
  preserves source offsets and page numbers for citations.
- Output: normalized passage records `{doc_id, passage_id, text, path, page, offset}`.

### 3.2 Incremental indexer
- `manifest.sqlite` stores per-file `(path, size, mtime, content_hash, indexed_version)`.
- On `reindex`: diff filesystem vs manifest → classify **added / modified / deleted** →
  only re-extract & re-embed the delta; delete stale passages from both indices.
- Daily cadence via a launchd/cron entry (and an on-demand `reindex` MCP tool).
- Crash-safe: write to temp index segment, atomically swap, then update manifest.

### 3.3 Sparse retriever (Pyserini)
- BM25 over passages; store passage metadata for snippet/citation rendering.
- Optional **PRF / query expansion** (RM3 — available in Pyserini) toggled per query.
- Handles short keyword queries well.

### 3.4 Dense retriever (Zvec + embedder)
- **Embedder interface** (`embed(texts) -> np.ndarray`) with implementations:
  - `Qwen3-Embedding-0.6B` (default), `Harrier-oss-0.6b`, easily extensible.
  - Runs on MPS; batched; embedding cache keyed by passage hash to avoid recompute.
- **Zvec** holds passage vectors + ids; ANN search returns top-k passages.
- Handles long/semantic queries well.

### 3.5 Knowledge-base retriever (Obsidian)
- Wrap **Obsidian CLI** to search a vault and traverse note links (graph hops).
- Returns notes + linked context; complements file search with curated knowledge.

### 3.6 Fusion layer
- Combine results across retrievers with **Reciprocal Rank Fusion (RRF)** (no tuning needed).
- Optional cross-encoder reranker as a later enhancement.
- Dedup by `doc_id`/passage; return unified ranked list with provenance per result.

### 3.7 MCP server
Tools exposed to agents:
- `search_sparse(query, k, prf=False)`
- `search_dense(query, k)`
- `search_kb(query, k, hops=1)`
- `search_hybrid(query, k, retrievers=[...], fusion="rrf")`
- `index_status()` — files indexed, last update, staleness
- `reindex(paths=None, full=False)`
Each result: `{path, page, snippet, score, retriever, doc_id}` for easy agent citation.
Also ship a thin **CLI** (`infogrep index|search|status`) sharing the same core, for
manual use and as the launchd entry point.

---

## 4. Repository layout

```
infogrep/
├── pyproject.toml              # uv-managed
├── infogrep/
│   ├── config.py               # config model + per-dir config.toml loader
│   ├── manifest.py             # sqlite manifest, change detection
│   ├── ingest/
│   │   ├── walker.py
│   │   ├── extract/            # pdf.py, docx.py, pptx.py, registry.py
│   │   └── chunker.py
│   ├── retrieval/
│   │   ├── base.py             # Retriever protocol + Result dataclass
│   │   ├── sparse.py           # Pyserini
│   │   ├── dense.py            # Zvec
│   │   ├── kb.py               # Obsidian
│   │   ├── embedders/          # base.py, qwen.py, harrier.py
│   │   └── fusion.py           # RRF
│   ├── indexer.py              # orchestrates ingest→index, incremental
│   ├── mcp_server.py           # MCP tool definitions
│   └── cli.py
├── scripts/launchd/            # daily reindex agent (.plist)
└── tests/
```

---

## 5. Milestones

### M0 — Scaffold (½ day)
`uv` project, package skeleton, config model, `git init`, CI lint/test, README.
**Done when:** `infogrep --help` runs; empty test suite passes.

### M1 — Ingestion + manifest (2–3 days)
Walker, extractor registry (PDF/DOCX/PPTX/MD/TXT first), chunker, `manifest.sqlite`,
change detection. **Done when:** `infogrep index <dir>` produces passages + manifest
for a mixed-file corpus, and a second run is a no-op (incremental verified).

### M2 — Sparse retrieval (2 days)
Pyserini index build from passages; `search_sparse` end-to-end; snippet rendering.
**Done when:** keyword query returns ranked passages with correct paths/pages.

### M3 — Dense retrieval (2–3 days)
Embedder interface + Qwen default, embedding cache, Zvec store, `search_dense`.
**Done when:** semantic query beats sparse on a paraphrase test case; RAM stays bounded.

### M4 — Fusion + MCP server (2 days)
RRF fusion, `search_hybrid`, full MCP server with all tools; wire into Claude Code/Codex.
**Done when:** an agent can call each tool and the hybrid tool over a real directory.

### M5 — Knowledge base (1–2 days)
Obsidian CLI wrapper, `search_kb` with graph hops, fold into fusion.
**Done when:** vault notes + linked context appear in hybrid results.

### M6 — Incremental scheduling + polish (1–2 days)
launchd daily reindex, `index_status`/`reindex` tools, PRF toggle, OCR fallback,
exclude rules, docs. **Done when:** daily auto-update works; staleness reported.

### Later / optional
Cross-encoder reranker; PRF tuning; multi-directory registry; result caching; eval harness.

---

## 6. Key risks & mitigations
- **JVM footprint (Pyserini/Tika)** → isolate Java to indexing; prefer `pymupdf` over Tika; document JDK setup.
- **Memory blow-up on large corpora** → stream extraction, batch embeddings, disk-backed indices, never hold full corpus in RAM (core constraint).
- **Zvec API/quirks** → still kept behind the `Retriever` interface for clean code, but **Zvec is the committed dense store** — no fallback backend is planned. Pin a known-good Zvec version and add an integration smoke test against it early (M3).
- **Embedding cost on re-index** → cache embeddings by passage content hash; only embed deltas.
- **Scanned PDFs** → optional OCR path, off by default for speed.

---

## 7. Immediate next steps
1. Scaffold the `uv` project and package skeleton (M0).
2. Build the extractor registry + chunker + manifest (M1) — the foundation everything else reads from.
3. Stand up Pyserini sparse search (M2) as the first working end-to-end slice.
