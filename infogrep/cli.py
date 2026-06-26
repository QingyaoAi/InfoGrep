"""InfoGrep command-line interface.

Thin wrapper over the core engine; also the entry point used by the daily
scheduled re-index. Subcommands are stubbed until their milestones land.
"""

from __future__ import annotations

import time
from pathlib import Path

import typer

from . import __version__
from .config import Config

app = typer.Typer(
    add_completion=False,
    help="Local-first content search (sparse + dense + knowledge base) for coding agents.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"infogrep {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """InfoGrep: index and search the content of local files."""


@app.command()
def index(
    directory: Path = typer.Argument(..., help="Directory to index."),
    full: bool = typer.Option(False, "--full", help="Force a full re-index."),
) -> None:
    """Build or incrementally update the side-car index for a directory."""
    from .indexer import Indexer

    cfg = Config.load(directory)
    if not cfg.target_dir.is_dir():
        typer.echo(f"[infogrep] not a directory: {cfg.target_dir}", err=True)
        raise typer.Exit(code=2)

    typer.echo(f"[infogrep] indexing {cfg.target_dir} -> {cfg.sidecar_dir}")
    report = Indexer(cfg).reindex(full=full)
    typer.echo(
        "[infogrep] "
        f"added={report.added} modified={report.modified} deleted={report.deleted} "
        f"unchanged={report.unchanged} skipped={report.skipped}"
    )
    typer.echo(f"[infogrep] index now holds {report.n_files} files, {report.n_passages} passages")
    for err in report.errors:
        typer.echo(f"[infogrep] error: {err}", err=True)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    directory: Path = typer.Option(Path.cwd(), "--dir", "-d", help="Indexed directory."),
    k: int = typer.Option(10, "--k", help="Number of results."),
    mode: str = typer.Option("hybrid", "--mode", "-m", help="hybrid | sparse | dense | kb."),
    prf: bool = typer.Option(False, "--prf", help="RM3 pseudo-relevance feedback (sparse)."),
) -> None:
    """Query indexed content."""
    from .engine import SearchEngine

    engine = SearchEngine(Config.load(directory))

    try:
        if mode == "sparse":
            results = engine.search_sparse(query, k=k, prf=prf)
        elif mode == "dense":
            results = engine.search_dense(query, k=k)
        elif mode == "hybrid":
            out = engine.search_hybrid(query, k=k, prf=prf)
            results = out.results
            if out.used:
                typer.echo(f"[infogrep] fused: {', '.join(out.used)}")
            for name, reason in out.skipped.items():
                typer.echo(f"[infogrep] skipped {name}: {reason}")
        elif mode == "kb":
            results = engine.search_kb(query, k=k)
        else:
            typer.echo(f"[infogrep] unknown mode: {mode}", err=True)
            raise typer.Exit(code=2)
    except FileNotFoundError as exc:
        typer.echo(f"[infogrep] {exc}", err=True)
        raise typer.Exit(code=2)

    if not results:
        typer.echo("[infogrep] no results.")
        return
    for i, r in enumerate(results, start=1):
        loc = f"{r.path}" + (f" p.{r.page}" if r.page is not None else "")
        typer.echo(f"{i:2}. [{r.score:.3f}] {loc}  ({r.retriever})")
        typer.echo(f"    {r.snippet.strip()[:160]}")


@app.command()
def status(
    directory: Path = typer.Argument(Path.cwd(), help="Indexed directory."),
) -> None:
    """Show index status and staleness for a directory."""
    from .indexer import Indexer

    cfg = Config.load(directory)
    info = Indexer(cfg).status()
    typer.echo(f"[infogrep] target: {cfg.target_dir}")
    if not info.get("indexed"):
        typer.echo("[infogrep] indexed: no")
        typer.echo("[infogrep] run `infogrep index <dir>` to build the index.")
        return
    typer.echo("[infogrep] indexed: yes")
    typer.echo(f"[infogrep] files: {info['n_files']}  passages: {info['n_passages']}")
    typer.echo(f"[infogrep] index version: {info['index_version']}")
    last = info.get("last_indexed_at")
    if last:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(last)))
        typer.echo(f"[infogrep] last indexed: {when}")
    if info.get("stale"):
        typer.echo(
            f"[infogrep] STALE: {info['pending']} pending "
            f"(+{info['pending_added']} ~{info['pending_modified']} -{info['pending_deleted']}) "
            "— run `infogrep index`"
        )
    elif "stale" in info:
        typer.echo("[infogrep] up to date")


@app.command()
def mcp(
    directory: Path = typer.Option(Path.cwd(), "--dir", "-d", help="Default indexed directory."),
) -> None:
    """Run the MCP server (stdio) so coding agents can call InfoGrep's search tools."""
    from .mcp_server import main as serve

    serve(directory=str(Path(directory).expanduser().resolve()))


schedule_app = typer.Typer(help="Manage daily auto-reindex (macOS launchd).")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("install")
def schedule_install(
    directory: Path = typer.Argument(..., help="Directory to reindex daily."),
    at: str = typer.Option("03:00", "--at", help="Daily run time, HH:MM (24h)."),
) -> None:
    """Install a daily reindex agent for a directory."""
    from . import scheduler

    try:
        hour, minute = (int(x) for x in at.split(":", 1))
    except ValueError:
        typer.echo(f"[infogrep] invalid --at time: {at!r} (use HH:MM)", err=True)
        raise typer.Exit(code=2)
    path = scheduler.install(directory, hour=hour, minute=minute)
    typer.echo(f"[infogrep] scheduled daily reindex of {Path(directory).resolve()} at {at}")
    typer.echo(f"[infogrep] launchd agent: {path}")


@schedule_app.command("uninstall")
def schedule_uninstall(
    directory: Path = typer.Argument(..., help="Directory whose schedule to remove."),
) -> None:
    """Remove the daily reindex agent for a directory."""
    from . import scheduler

    if scheduler.uninstall(directory):
        typer.echo(f"[infogrep] removed reindex schedule for {Path(directory).resolve()}")
    else:
        typer.echo("[infogrep] no schedule found for that directory.")


@schedule_app.command("list")
def schedule_list() -> None:
    """List installed daily reindex agents."""
    from . import scheduler

    agents = scheduler.list_agents()
    if not agents:
        typer.echo("[infogrep] no reindex schedules installed.")
        return
    for a in agents:
        typer.echo(f"[infogrep] {a['hour']:02d}:{a['minute']:02d} daily  {a['directory']}")


if __name__ == "__main__":  # pragma: no cover
    app()
