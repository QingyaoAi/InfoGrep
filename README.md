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
infogrep status <dir>                 # show index status / staleness
infogrep mcp --dir <dir>             # run the MCP server (stdio) for coding agents
```

Indices live in a `<dir>/.infogrep/` side-car; original files are never modified.

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

`search_kb` adds graph-aware search over an Obsidian vault: it matches notes by
content/title/tags, then expands along `[[wikilinks]]` (both directions) so notes
*connected* to a match surface too. Enable it per indexed directory in
`<dir>/.infogrep/config.toml`:

```toml
[kb]
enabled = true
vault_path = "/path/to/ObsidianVault"
hops = 1
```

The vault is read live at query time — no separate index, always current.
