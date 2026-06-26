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
`search_hybrid`, `index_status`, `reindex`. Register it with Claude Code:

```bash
claude mcp add infogrep -- uv run infogrep mcp --dir /path/to/your/project
```

The search tools return `{"results": [...]}` where each result carries
`path`, `page`, `snippet`, `score`, and `retriever` for easy citation.
`search_hybrid` (recommended) fuses sparse + dense with reciprocal rank fusion and
reports which retrievers were `used`/`skipped`.
