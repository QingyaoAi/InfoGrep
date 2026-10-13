# InfoGrep

[![CI](https://github.com/QingyaoAi/InfoGrep/actions/workflows/ci.yml/badge.svg)](https://github.com/QingyaoAi/InfoGrep/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**InfoGrep is a local-first search engine for the *content* of your files.** Point it at a
folder — a Dropbox, a codebase, a research archive — and it indexes what's actually written
inside every PDF, Office doc, spreadsheet, note, and image caption, then makes it searchable
by keyword, meaning, or knowledge-graph — from the command line, a browser, or directly as
tools your coding agent (Claude Code, Codex, DeepSeek Harness, …) can call.

Everything runs on your machine. Nothing is uploaded anywhere. Your files are never modified.

New in 1.0: **ask a question instead of searching.** With
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) installed, the launcher,
the browser UI and `infogrep ask` hand your question to an agent that searches and reads
your files and answers with its sources — see [Ask a question](#ask-a-question-deepseek-harness).
That one feature is the exception to "nothing is uploaded": the passages the agent reads go
to the model that writes the answer.

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

Pick one of three routes. All of them give you the same search engine; they differ in what
they set up around it.

| Route | For | You get |
|---|---|---|
| [Standalone app](#standalone-app-macos-no-dependencies) | macOS (Apple Silicon), nothing to install | Menu-bar launcher with its own bundled backend |
| [`./install.sh`](#installsh-backend--agent-harnesses-all-platforms-macos-app-on-macos) | macOS or Linux, from a checkout | CLI, server at login + launcher (macOS), MCP registered with your agents, semantic search |
| [`pip install infogrep`](#pip) | macOS or Linux, just the engine | CLI, `infogrep serve`, `infogrep mcp` |

Question answering works with every route once DeepSeek Harness is set up — that part is
the same for all three and is described in
[Set up question answering](#set-up-question-answering-deepseek-harness) below.

### Standalone app (macOS, no dependencies)

1. Download `InfoGrep.dmg` from the latest
   [GitHub release](https://github.com/QingyaoAi/InfoGrep/releases) and drag InfoGrep to
   `/Applications`. (`InfoGrep.app.zip` is the same bundle.)
2. Clear the download flag once — the app is ad-hoc signed, not notarized:

   ```bash
   xattr -dr com.apple.quarantine /Applications/InfoGrep.app
   ```

   Or open it, let macOS refuse, then **System Settings → Privacy & Security → Open Anyway**.
3. Open it. A magnifier icon appears in the menu bar; choose **Index a Folder…** there.
4. Press **⌘⇧Space** and type.

It bundles Python, InfoGrep and a Java runtime, and starts its own local server, so there is
no `uv`, Python or JDK to install. Keyword search downloads its engine (one ~112 MB jar) the
first time you index. Semantic (dense) search is not in the bundle — torch is too big to
ship — so it searches with the keyword, folder and knowledge-base retrievers.

### pip

```bash
pip install infogrep            # add [dense] for semantic search: pip install 'infogrep[dense]'
infogrep index ~/Documents
infogrep search "quarterly OKR draft" -d ~/Documents
infogrep serve --dir ~/Documents   # browser UI at http://127.0.0.1:7421
```

Needs Python 3.10+ and JDK 21 for keyword search (table below). Nothing is started for you
and no agent is configured; see [MCP server](#mcp-server-claude-code--codex--deepseek-harness)
to register it by hand.

### From a checkout

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
on either OS. `./install.sh` (below) additionally wires up autostart and your agent harness.

### `./install.sh` (backend + agent harnesses, all platforms; macOS app on macOS)

```bash
./install.sh          # INFOGREP_SERVE_DIR=/path sets the default folder; INFOGREP_PORT changes the port
```

This always: runs `uv sync --extra dense`, checks for JDK 21, and registers the `infogrep` MCP server with
Claude Code (if `claude` is on `PATH`) and DeepSeek Harness (if `dsh` is).

**On macOS**, it also builds a Spotlight-style menu-bar app and installs `launchd` login
agents that start the app and the web UI (search backend) at login. Daily incremental
reindexing is opt-in per folder: toggle it in the web UI or the menu-bar app, or use
`infogrep schedule install/uninstall/list`. Press **⌘⇧Space** for the
launcher, or open <http://127.0.0.1:7421>; add folders to search from the app (**Index a
Folder…**) or the web UI (**＋ folder**). Additionally requires Xcode Command Line Tools
(`xcode-select --install`). An app you built yourself isn't quarantined, so it just opens.

Don't want to install anything? Use the
[standalone app](#standalone-app-macos-no-dependencies) instead.

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

### Set up question answering (DeepSeek Harness)

Optional, and the same whichever way you installed InfoGrep. Asking a question runs a
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) agent, so `dsh`
has to be installed and able to reach a model. InfoGrep needs nothing configured on its own
side: once `dsh` works, the **Ask** button and `infogrep ask` do.

**1. Install Node.js and `dsh`.** `dsh` needs Node.js 22.19+ or 24+ (`brew install node`,
or [nodejs.org](https://nodejs.org)).

```bash
npm install -g @deepseek-ai/dsh
dsh --version
```

**2. Give `dsh` a model.** Start its web UI once, open **Settings → Models**, paste a
[DeepSeek API key](https://platform.deepseek.com/) and save. The key is stored by `dsh`
(under `~/.dsh`), not by InfoGrep, and every `dsh` profile uses it.

```bash
dsh web          # opens http://127.0.0.1:3080 — close it again once the key is saved
```

**3. Check that `dsh` answers on its own.** This is exactly the mode InfoGrep drives:

```bash
dsh headless "Reply with the single word: ready"
```

If that prints `ready`, you are done with `dsh`. (The first run also creates the `headless`
profile that InfoGrep builds on.)

**4. Ask.** Index a folder if you haven't, then use whichever front end you have:

| Where | How |
|---|---|
| Launcher (⌘⇧Space) | Click **Ask** next to the search field, or press **Tab**. Type the question, **↵**. **⇧↵** asks a follow-up, **Tab** returns to search. |
| Browser UI | Pick the **Ask** mode beside Hybrid / Keyword / …, type, **Ask**. |
| Terminal | `infogrep ask "your question"` (`--dir` for one folder, `--continue` to follow up) |

The launcher and the browser UI only show **Ask** once the InfoGrep server has found `dsh`.
The launcher asks again each time you open its menu-bar menu, and the browser UI on every page
load, so after installing `dsh` there is nothing to restart.

**If there is no Ask button**, the server did not find `dsh`. A server started at login or
from the app does not get your shell's `PATH`; besides its own, it looks in
`/opt/homebrew/bin`, `/usr/local/bin` and `~/.local/bin`. Homebrew's Node puts `dsh` in the
first of those. With nvm, volta or fnm it lands somewhere else — link it into one of the
three, and node is then found beside the real `dsh`:

```bash
mkdir -p ~/.local/bin && ln -s "$(command -v dsh)" ~/.local/bin/dsh
```

**If Ask answers with an error**, run step 3 again in a terminal: nearly every failure
(no API key, no credit, no network) is `dsh`'s own and shows up there with its message.

**Using `dsh` yourself, with InfoGrep as a tool.** Separate from Ask: `infogrep dsh install
--dir <folder>` adds InfoGrep's search tools to your own `dsh web` sessions (see
[MCP server](#mcp-server-claude-code--codex--deepseek-harness)). Ask does not depend on it,
and it does not depend on Ask.

## Usage

### CLI

```bash
infogrep index <dir>                 # build / update the index for a directory
infogrep search <query> -d <dir>     # query (modes: hybrid [default] | sparse | dense | kb | graph)
infogrep search <query> --prf        # sparse query expansion (RM3)
infogrep ask "<question>"            # have a DeepSeek Harness agent answer from your files
infogrep learn <query> -d <dir>      # distill search results into linked KB vault notes
infogrep status <dir>                # index status + staleness (pending changes)
infogrep mcp --dir <dir>             # run the MCP server (stdio) for coding agents
infogrep serve --dir <dir>           # browser UI to test search (http://127.0.0.1:7421)
infogrep schedule install <dir> --at 03:00   # daily auto-reindex via launchd (macOS only)
infogrep schedule list | uninstall <dir>
```

`infogrep status <dir>` prints the exact index location and reports **staleness** — files
added/modified/deleted since the last index — so you know when a manual `infogrep index` is
worth running.

### MCP server (Claude Code / Codex / DeepSeek Harness)

Register InfoGrep as an MCP server so an agent can search your files as a tool call:

```bash
claude mcp add infogrep -- uv run infogrep mcp --dir /path/to/your/project
```

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) has no such
command — a profile loads its MCP servers from `$DSH_HOME/profiles/<name>/cordis.patch.yml` —
so InfoGrep writes the entry for you:

```bash
infogrep dsh install --dir /path/to/your/project   # into the `web` profile (what `dsh web` boots)
infogrep dsh install --dir … --profile headless    # or another profile; repeatable
infogrep dsh list                                   # which profiles have it
infogrep dsh uninstall
```

New sessions of that profile then see the tools as `mcp__infogrep__search_hybrid` and so on.
The command only ever touches its own marked block in the patch file, checks that `dsh` still
loads the result (restoring the file if not), and leaves an `infogrep` server you added by
hand exactly as it is.

Tools exposed: `search_sparse`, `search_dense`, `search_kb`, `search_graph`,
`search_hybrid`, `read_file`, `kb_learn`, `index_status`, `reindex`. Each search tool returns
`{"results": [...]}` where every result carries `path`, `page`, `snippet`, `score`, and
`retriever` for easy citation. `search_hybrid` (recommended) fuses whichever retrievers are
enabled and reports which were `used` vs. `skipped` (and why). `read_file` returns the text
behind a result the way InfoGrep extracts it — PDF, Word, slides and spreadsheets included —
for files inside an indexed directory.

`infogrep mcp --read-only` leaves out the two tools that write (`reindex`, `kb_learn`), and
`--all-folders` makes a search that names no directory cover every indexed folder.

When `infogrep serve` is running, the MCP server hands its searches to it instead of opening
the indexes a second time: the server already has the JVM and the embedding model loaded, so
each agent session costs a small Python process rather than its own copy of both. It only
does so for a server of the same version reading the same `$INFOGREP_HOME`; without one the
search runs in the MCP process as before. `INFOGREP_SERVER` names the server
(`http://127.0.0.1:7421` by default); set it empty to always search in-process.

### Ask a question (DeepSeek Harness)

For a question a list of hits doesn't answer — one that needs several searches, reading the
files, and putting it together — InfoGrep can hand it to a
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) agent:

```bash
infogrep ask "Which of my proposals mention a budget above 500k, and for what?"
infogrep ask "…" --dir /path/to/one/folder    # default: every indexed folder
infogrep ask --continue "And which of those were funded?"   # follow up on the last answer
infogrep ask "…" --notes                      # also let it search the Obsidian knowledge base
```

The same thing is in the macOS launcher (the **Ask** button, or **Tab**) and in the browser
UI (the **Ask** mode). It needs `dsh` installed with a model configured —
[Set up question answering](#set-up-question-answering-deepseek-harness) walks through it;
nothing else is set up, and `infogrep dsh install` is not required.

- **On demand.** A question starts one `dsh headless` process, which searches with the tools
  above, reads what it needs, answers, and exits. Nothing runs between questions, and the
  server answers one question at a time (asking another stops the one in flight). A question
  still unanswered after 10 minutes is stopped.
- **No knowledge base unless you say so.** The Obsidian vault answers through the Obsidian
  app, far slower than the indexes, and an agent searches many times per question. So it is
  left out — of the fused search and as a tool — unless you pass `--notes`, tick **Notes** in
  the launcher, or tick *include notes* in the browser UI.
- **Read-only.** The agent runs under dsh's read-only file policy with nobody to approve an
  exception, and from InfoGrep it gets the search tools and `read_file`, not `reindex` or
  `kb_learn`. It can read your files and change none of them.
- **Not local.** Search never leaves your machine; an answer does. It is written by the model
  dsh is set up with, and the passages the agent reads are sent to that model's API.
- **Self-contained.** No dsh profile is edited: the agent's setup is a patch overlay passed
  on the command line. dsh never deletes a session, so the overlay also points its session
  log at `~/.infogrep/ask/` instead of `~/.dsh/`, where InfoGrep deletes conversations a week
  after they were last used. `./uninstall.sh` removes that folder.

### Browser UI

```bash
infogrep serve --dir <dir>    # http://127.0.0.1:7421 by default
```

A search box, a mode selector (hybrid/sparse/dense/kb/graph, plus **Ask** when `dsh` is
installed), result snippets with path/page/score, folder management (add/switch indexed
directories), and a JSON API at `/api/search` and `/api/status`. Bound to localhost only.

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

### Building the knowledge base from your files (`learn`)

`infogrep learn <query>` (MCP: `kb_learn`) is the write side of the knowledge base: it
searches the indexed files (sparse + dense + graph — never kb itself), mines the top
passages for related entities (capitalized phrases and acronyms, ranked by
cross-document frequency — model-free and local like everything else), and writes
interlinked notes into the vault's agent folder (`kb.agent_folder`, default
`"Agent Knowledge"` — the only vault location InfoGrep ever writes to):

```
Agent Knowledge/Topics/<query>.md      source paths + snippets + [[entity]] links
Agent Knowledge/Entities/<entity>.md   one note per entity, linking back to its topics
```

Because the notes are joined by wikilinks, a later `search_kb` that hits any of them
expands along links/backlinks and surfaces the whole neighborhood — topic, entities,
and source citations — without re-searching the files. Rebuilding a topic is
idempotent: the topic note is regenerated in place and entity notes never gain
duplicate mentions.

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
agent_folder = "Agent Knowledge"   # vault folder `infogrep learn` writes into

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
