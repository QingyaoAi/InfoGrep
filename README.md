# InfoGrep

A local-first tool that searches the **actual content** of every file in a directory and
exposes **sparse**, **dense**, and **knowledge-base** retrieval to coding agents
(Claude Code, Codex) via an MCP server — separately or fused.

See [PLAN.md](PLAN.md) for the full design and milestones.

## Status

Early scaffold (M0). Command surface is stubbed; retrieval lands in later milestones.

## Quick start

```bash
uv sync --extra dev      # create venv + install deps
uv run infogrep --help   # show command surface
uv run pytest            # run tests
```

## Commands

```bash
infogrep index <dir>                 # build / update the side-car index for a directory
infogrep search <query> -d <dir>     # query (modes: hybrid [default] | sparse | dense)
infogrep search <query> --prf        # sparse query expansion (RM3)
infogrep status <dir>                 # show index status + staleness (pending changes)
infogrep mcp --dir <dir>             # run the MCP server (stdio) for coding agents
infogrep serve --dir <dir>          # browser UI to test search (http://127.0.0.1:7421)
infogrep schedule install <dir> --at 03:00   # daily auto-reindex via launchd
infogrep schedule list | uninstall <dir>
```

Indices live in a `<dir>/.infogrep/` side-car; original files are never modified.

Sparse search is **multi-field**: it matches the query against the passage text *and*
the file name and path (tokenized), with configurable boosts (`[sparse] field_boosts`),
so you can find a file by its name/path, not only its contents. Results always include
the original absolute path and file metadata (`abs_path`, `filename`, `ext`, `size`, `mtime`).

**Sparse** (BM25) is on by default. **Dense** (embedding) retrieval is **off by default**
— it needs a model download and significant RAM/GPU — enable it per directory with
`[dense] enabled = true` in `.infogrep/config.toml`. With dense off, `hybrid` simply runs
sparse (plus the knowledge base, if enabled).

## Daily auto-reindex

`infogrep schedule install <dir>` registers a macOS launchd agent that reindexes the
directory once a day (logs to `<dir>/.infogrep/reindex.log`). `infogrep status` reports
**staleness** — how many files are added/modified/deleted since the last index — so you
know when a manual `infogrep index` is due.

## Scanned PDFs (OCR)

PDFs with no text layer (scans) can be OCR'd at ingest time. Requires `tesseract`.
Enable per directory in `.infogrep/config.toml`:

```toml
[ingest]
ocr = true          # OCR pages with little/no extractable text
ocr_min_chars = 16  # threshold below which a page is OCR'd
```

## Browser UI

`infogrep serve --dir <dir>` starts a small local web UI (default
`http://127.0.0.1:7421`, an uncommon port; override with `--port`) for testing search by
hand — a search box, a mode selector (hybrid/sparse/dense/kb), result snippets with
path/page/score, and a JSON API at `/api/search` and `/api/status`. Bound to localhost.

## MCP server (Claude Code / Codex)

InfoGrep exposes its retrieval as MCP tools — `search_sparse`, `search_dense`,
`search_kb`, `search_hybrid`, `index_status`, `reindex`. Register it with Claude Code:

```bash
claude mcp add infogrep -- uv run infogrep mcp --dir /path/to/your/project
```

The search tools return `{"results": [...]}` where each result carries
`path`, `page`, `snippet`, `score`, and `retriever` for easy citation.
`search_hybrid` (recommended) fuses the enabled retrievers with reciprocal rank fusion
and reports which were `used`/`skipped`.

## Knowledge base (Obsidian vault)

`search_kb` adds graph-aware search over an Obsidian vault via the **Obsidian CLI**:
it `search`es the live vault, then expands along `links`/`backlinks` so notes
*connected* to a match surface too. Requires the Obsidian app running with the vault
open. Enable it per indexed directory in `<dir>/.infogrep/config.toml`:

```toml
[kb]
enabled = true
vault = "My Vault"   # Obsidian vault name; omit to use the CLI's active vault
hops = 1             # link hops to expand (follows links + backlinks)
# cli = "obsidian"   # path to the Obsidian CLI, if not on PATH
```

The vault is queried live — no separate index, always current. If the app isn't
running, `search_kb` is skipped (in hybrid) or reports a clear error (standalone).
