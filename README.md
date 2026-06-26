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
infogrep index <dir>     # build / update the side-car index for a directory
infogrep search <query>  # query indexed content
infogrep status <dir>    # show index status / staleness
```

Indices live in a `<dir>/.infogrep/` side-car; original files are never modified.
