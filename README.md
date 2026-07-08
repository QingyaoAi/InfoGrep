# InfoGrep

[![CI](https://github.com/QingyaoAi/InfoGrep/actions/workflows/ci.yml/badge.svg)](https://github.com/QingyaoAi/InfoGrep/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**InfoGrep is a local-first search engine for the *content* of your files.** Point it at a
folder — a Dropbox, a codebase, a research archive — and it indexes what's actually written
inside every PDF, Office doc, spreadsheet, note, and image caption, then makes it searchable
by keyword, meaning, or knowledge-graph — from the command line, a browser, or directly as
tools your coding agent (Claude Code, Codex, …) can call.

Everything runs on your machine. Nothing is uploaded anywhere. Your files are never modified.

📖 **[Project website](https://QingyaoAi.github.io/InfoGrep/)** · [PLAN.md](PLAN.md) (design & milestones)

---

## Why InfoGrep

`grep` and Spotlight only see file names, or plain text. They can't look inside a PDF, DOCX,
or PPTX, they don't rank results by relevance, and they have no idea what a coding agent
should do with the output. InfoGrep fixes all three:

- **Reads real content.** PDFs (including scanned ones, via OCR), Word/PowerPoint/Excel,
  legacy `.doc`, RTF/OpenDocument, plain text and markup, and JSON — not just file names.
- **Four complementary retrieval modes, fused.** Exact-keyword (BM25), semantic
  (embeddings), an Obsidian knowledge-base graph, and a folder/filename metadata graph —
  combined with reciprocal rank fusion, or called independently.
- **Folder-aware, not just file-aware.** A metadata graph over your folder structure (paths
  and file names only, never content) lets hybrid search also surface sibling files from the
  *folder* a hit lives in — not only files whose own content matched the query.
- **Built for agents, not just humans.** An MCP server exposes each retriever as a tool with
  structured, citable results (`path`, `page`, `snippet`, `score`), so Claude Code, Codex, or
  any MCP-aware agent can search your files as naturally as it reads them.
- **Local-first and non-destructive.** The index lives in a side-car location outside the
  folder you're searching; your files are only ever read.
- **Incremental.** Re-indexing only touches files that changed since the last run, and can
  run on a daily schedule automatically.

## How it works

```
                    ┌───────────────────────────────────────────────┐
                    │        MCP server  /  CLI  /  browser UI       │
                    │  search_sparse · search_dense · search_kb      │
                    │  search_graph · search_hybrid                  │
                    │  index_status · reindex                        │
                    └───────────────────────┬─────────────────────────┘
                                            │
        ┌───────────────┬───────────────────┼────────────────┬───────────────┐
        │                │                   │                │
 ┌──────▼──────┐  ┌──────▼──────┐    ┌───────▼──────┐  ┌───────▼──────┐
 │   Sparse    │  │    Dense    │    │  Knowledge   │  │    Folder    │
 │  (Pyserini  │  │ (embeddings │    │     base     │  │   metadata   │
 │    BM25,    │  │ + Zvec ANN, │    │  (Obsidian   │  │    graph     │
 │  bilingual) │  │off by       │    │ graph, live  │  │ (paths only, │
 │             │  │ default)    │    │    vault)    │  │ no content)  │
 └──────┬──────┘  └──────┬──────┘    └───────┬──────┘  └───────┬──────┘
        └───────────────┴───────────────────┴────────────────┘
                                  │  reciprocal rank fusion
                           ┌──────▼──────┐
                           │   Fusion    │
                           └──────┬──────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │   Side-car index store      │
                    │  ~/.infogrep/indexes/<dir>/  │
                    └─────────────▲──────────────┘
                                  │
      ┌───────────────────────────┴────────────────────────────┐
      │  Ingestion pipeline                                     │
      │  walk (include/exclude globs) → extract (per file type) │
      │       → chunk into passages → index (sparse/dense)      │
      │       → build folder/filename metadata graph             │
      │       → manifest.sqlite tracks hash/mtime for deltas    │
      └──────────────────────────────────────────────────────────┘
```

1. **Walk** the target directory, respecting include/exclude glob patterns.
2. **Extract** text per file type (PDF via PyMuPDF, DOCX/PPTX/XLSX via python-docx/pptx/
   openpyxl, legacy `.doc` via macOS `textutil` — no content extractor for `.doc` on Linux
   yet, everything else as UTF-8 text). Files with no extractable content are still indexed
   by file name/path, so they're findable.
3. **Chunk** long documents into overlapping passages (`{doc_id, passage_id, text, path,
   page}`), preserving page numbers for citations.
4. **Index** passages into a **manifest** (SQLite: path → hash/mtime/size, for change
   detection) plus **sparse** (Lucene/BM25 via Anserini) and, optionally, **dense**
   (embeddings in a Zvec vector store) indexes.
5. **Build the folder/filename metadata graph** from every indexed file's *path* (never its
   content): a folder tree materialized as an Obsidian-compatible vault of linked notes
   (browsable in Obsidian) plus a compact JSON form used for fast lookups.
6. **Retrieve** via any of the four retrievers, or all of them fused with **reciprocal rank
   fusion (RRF)** — no tuning required, and each retriever can be skipped gracefully if it
   isn't enabled or available. The metadata graph lets a hit's *folder* pull in sibling files
   too, not only files whose own content matched.
7. **Re-index incrementally**: a manifest diff classifies files as added/modified/deleted, so
   only the delta is re-extracted, re-chunked, and re-indexed — a no-op run does nothing (the
   metadata graph rebuilds whenever files are added/removed — cheap, since it's just paths).

The index is **never** written into the folder you're searching — it lives under
`$INFOGREP_HOME/indexes/<name>-<hash>/` (default `~/.infogrep`), so your directory's
structure and git history stay untouched.

## What it can search

| Category | Types |
|---|---|
| Documents | `pdf` `doc` `docx` `ppt` `pptx` `xls` `xlsx` `rtf` `odt` `ods` `odp` |
| Text & markup | `txt` `md` `markdown` `rst` `tex` `csv` `tsv` `json` `jsonl` |
| Images (name/path; content with OCR) | `png` `jpg` `jpeg` `gif` `bmp` `tif` `tiff` `webp` `svg` `heic` `heif` |

This is the default; set `include = ["**/*"]` in a directory's config to index every file
(anything without a dedicated extractor is still indexed by name/path). Dependency, VCS, and
cache trees (`node_modules`, `.git`, `.venv`, `__pycache__`, …) and editor/OS junk are
excluded by default.

Sparse search is **multi-field**: queries match passage text *and* the file name/path
(tokenized, independently weighted), so you can find a file by what it's called, not only
what it says. Sparse indexing is **bilingual by default** (`en+zh`): English gets Porter
stemming, Chinese/Japanese/Korean get CJK bigram analysis, in a single index — switch to a
single language with `[sparse] language`.

## Install

Works the same way on **macOS and Linux**. Requires [`uv`](https://docs.astral.sh/uv/) and
JDK 21 for sparse search:

| Platform | JDK 21 |
|---|---|
| macOS | `brew install openjdk@21` |
| Debian/Ubuntu | `sudo apt install openjdk-21-jdk` |
| Fedora/RHEL | `sudo dnf install java-21-openjdk` |
| Arch | `sudo pacman -S jdk-openjdk` |

JDK is auto-detected (`JAVA_HOME`, Homebrew/Linuxbrew, `/usr/lib/jvm`, `update-alternatives`,
`java` on `PATH`) — no manual `JAVA_HOME` wiring needed once it's installed. The Anserini
engine (a single ~112 MB jar) is downloaded from Maven Central on first index and cached
under `~/.infogrep/jars/` — the Python install itself stays small.

Dense (embedding) search is an optional extra — the base install skips the ~800 MB
torch/sentence-transformers stack. Add it with `uv sync --extra dense` (from a checkout) or
`pip install 'infogrep[dense]'`, then enable `[dense]` per directory in the config.

```bash
git clone https://github.com/QingyaoAi/InfoGrep.git
cd InfoGrep
uv sync --extra dev --extra dense   # create venv + install deps (drop --extra dense to slim)
uv run infogrep --help              # show command surface
uv run pytest                       # run tests
```

That's the whole backend — `uv run infogrep index/search/serve/mcp` all work at this point
on either OS. `./install.sh` (below) additionally wires up autostart and Claude Code.

### `./install.sh` (backend + Claude Code, all platforms; macOS app on macOS)

```bash
./install.sh          # INFOGREP_SERVE_DIR=/path sets the default folder; INFOGREP_PORT changes the port
```

This always: runs `uv sync --extra dense`, checks for JDK 21, and registers the `infogrep` MCP server with
Claude Code (if `claude` is on `PATH`).

**On macOS**, it also builds a Spotlight-style menu-bar app and installs `launchd` login
agents that start the app and the web UI (search backend) at login. Daily incremental
reindexing is opt-in per folder: toggle it in the web UI or the menu-bar app, or use
`infogrep schedule install/uninstall/list`. Press **⌘⇧Space** for the
launcher, or open <http://127.0.0.1:7421>; add folders to search from the app (**Index a
Folder…**) or the web UI (**＋ folder**). Additionally requires Xcode Command Line Tools
(`xcode-select --install`); the app is ad-hoc signed, so the first launch needs a right-click
→ **Open** (one time).

Don't want to compile the app? Every [GitHub release](https://github.com/QingyaoAi/InfoGrep/releases)
ships a prebuilt `InfoGrep.app.zip` — unzip, drag to `/Applications`, then right-click →
**Open** on first launch (it's ad-hoc signed; or `xattr -d com.apple.quarantine
/Applications/InfoGrep.app`). The app is just the launcher UI — it needs the backend running
(`infogrep serve`, or the login agent that `./install.sh` sets up).

**On Linux**, there's no menu-bar app and nothing is auto-started (no bundled systemd/cron
integration yet) — run the web UI yourself when you want it:

```bash
uv run infogrep serve --dir /path/to/folder   # http://127.0.0.1:7421
```

or wire up your own `systemd --user` unit / cron job if you want it running persistently or
on a schedule.

Remove everything cleanly:

```bash
./uninstall.sh            # removes the app, login agents and MCP server (keeps indexes)
./uninstall.sh --purge    # also delete all indexes (~/.infogrep)
```

`make install` / `make uninstall` / `make purge` are equivalent; run `make` to list all
targets (`sync`, `app`, `test`, `lint`, …).

## Usage

### CLI

```bash
infogrep index <dir>                 # build / update the index for a directory
infogrep search <query> -d <dir>     # query (modes: hybrid [default] | sparse | dense | kb | graph)
infogrep search <query> --prf        # sparse query expansion (RM3)
infogrep status <dir>                # index status + staleness (pending changes)
infogrep mcp --dir <dir>             # run the MCP server (stdio) for coding agents
infogrep serve --dir <dir>           # browser UI to test search (http://127.0.0.1:7421)
infogrep schedule install <dir> --at 03:00   # daily auto-reindex via launchd (macOS only)
infogrep schedule list | uninstall <dir>
```

`infogrep status <dir>` prints the exact index location and reports **staleness** — files
added/modified/deleted since the last index — so you know when a manual `infogrep index` is
worth running.

### MCP server (Claude Code / Codex)

Register InfoGrep as an MCP server so an agent can search your files as a tool call:

```bash
claude mcp add infogrep -- uv run infogrep mcp --dir /path/to/your/project
```

Tools exposed: `search_sparse`, `search_dense`, `search_kb`, `search_graph`,
`search_hybrid`, `index_status`, `reindex`. Each search tool returns `{"results": [...]}`
where every result carries `path`, `page`, `snippet`, `score`, and `retriever` for easy
citation. `search_hybrid` (recommended) fuses whichever retrievers are enabled and reports
which were `used` vs. `skipped` (and why).

### Browser UI

```bash
infogrep serve --dir <dir>    # http://127.0.0.1:7421 by default
```

A search box, a mode selector (hybrid/sparse/dense/kb/graph), result snippets with path/page/
score, folder management (add/switch indexed directories), and a JSON API at
`/api/search` and `/api/status`. Bound to localhost only.

### Folder/filename metadata graph

On every reindex, InfoGrep builds a knowledge graph over your folder structure — each file's
*path and name only, never its content* — and materializes it as an Obsidian-compatible vault
of linked folder notes under the index's `graph_vault/` side-car directory (open it in
Obsidian to browse, if you like). `search_graph` matches a query against folder/file *names*,
then expands to neighboring folders (parent, children, siblings) so files that live in the
most relevant folder(s) surface too — not just files whose own name/content matched. It
participates in `search_hybrid` automatically, letting one hit pull in its co-located
siblings. On by default (it's cheap — just path manipulation, no model or JVM):

```toml
[graph]
enabled = true    # set false to disable
hops = 1          # folder hops to expand from a matched folder (parent/children/siblings)
max_folders = 5   # top-scoring folders to expand into file candidates per query
```

### Knowledge base (Obsidian vault)

`search_kb` adds graph-aware search over an Obsidian vault via the **Obsidian CLI**: it
searches the live vault, then expands along links/backlinks so notes *connected* to a match
surface too — always current, no separate index. Requires the Obsidian app running with the
vault open. Enable per directory:

```toml
[kb]
enabled = true
vault = "My Vault"   # Obsidian vault name; omit to use the CLI's active vault
hops = 1              # link hops to expand (follows links + backlinks)
# cli = "obsidian"    # path to the Obsidian CLI, if not on PATH
```

If the app isn't running, `search_kb` is skipped (in hybrid) or reports a clear error
(standalone).

### Scanned PDFs (OCR)

PDFs with no text layer can be OCR'd at ingest time (requires `tesseract`):

```toml
[ingest]
ocr = true          # OCR pages with little/no extractable text
ocr_min_chars = 16  # threshold below which a page is OCR'd
```

### Daily auto-reindex

`infogrep schedule install <dir>` registers a macOS `launchd` agent that reindexes the
directory once a day (logs to the index dir's `reindex.log`). **macOS only** — on Linux, set
up your own cron job or systemd timer instead, e.g.:

```bash
# crontab -e
0 3 * * *  cd /path/to/InfoGrep && uv run infogrep index /path/to/dir
```

## Configuration reference

Config is TOML, read from (in order) a global `$INFOGREP_HOME/config.toml`, then a
per-directory override at that index's `config.toml` (path shown by `infogrep status`).

```toml
include = ["**/*.pdf", "**/*.docx", "..."]   # default: documents + images, see table above
exclude = ["**/node_modules/**", "..."]       # default: VCS/deps/cache/OS junk

[chunk]
size = 512      # target passage size (tokens/words)
overlap = 64    # overlap between adjacent passages

[ingest]
ocr = false          # OCR scanned PDF pages
ocr_min_chars = 16   # page text below this length triggers OCR
workers = 0          # parallel extraction processes; 0 = auto (min(8, cpu count))

[sparse]
enabled = true
prf = false            # RM3 pseudo-relevance feedback
prf_fb_docs = 10
prf_fb_terms = 10
language = "en+zh"     # "en" | "zh" | "ja" | "ko" | "en+zh" (changing re-indexes)
field_boosts = { contents = 1.0, filename = 2.0, pathtext = 1.0 }

[dense]
enabled = false                          # off by default: needs a model + RAM/GPU
embedder = "qwen"                        # registry key; see infogrep.retrieval.embedders
model_name = "Qwen/Qwen3-Embedding-0.6B"
device = "auto"                          # "auto" -> mps/cuda/cpu

[kb]
enabled = false
vault = ""      # Obsidian vault name; empty -> the CLI's active vault
cli = "obsidian"
hops = 1
search_limit = 10

[graph]
enabled = true    # folder/filename metadata graph; cheap, on by default
hops = 1          # folder hops to expand from a matched folder
max_folders = 5   # top-scoring folders to expand into file candidates per query
```

With dense off (the default), `hybrid` simply runs sparse and the metadata graph (plus the
knowledge base, if enabled) — no model download needed until you opt in.

## Development

```bash
make sync    # create/refresh the dev virtualenv
make test    # run the test suite
make lint    # ruff + shellcheck
make app     # build the macOS menu-bar app
```

See [PLAN.md](PLAN.md) for the full architecture write-up and milestone history.

## License

[MIT](LICENSE)
