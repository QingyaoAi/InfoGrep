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
    mode: str = typer.Option(
        "hybrid", "--mode", "-m", help="sparse | dense | kb | hybrid."
    ),
) -> None:
    """Query indexed content."""
    typer.echo(f"[infogrep] query={query!r} mode={mode} k={k} dir={directory}")
    typer.echo("[infogrep] search is not yet implemented (lands in M2-M5).")
    raise typer.Exit(code=1)


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


if __name__ == "__main__":  # pragma: no cover
    app()
