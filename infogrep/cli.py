"""InfoGrep command-line interface.

Thin wrapper over the core engine; also the entry point used by the daily
scheduled re-index. Subcommands are stubbed until their milestones land.
"""

from __future__ import annotations

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
    cfg = Config.load(directory)
    typer.echo(f"[infogrep] target: {cfg.target_dir}")
    typer.echo(f"[infogrep] side-car: {cfg.sidecar_dir}")
    typer.echo("[infogrep] indexing is not yet implemented (lands in M1-M3).")
    raise typer.Exit(code=1)


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
    cfg = Config.load(directory)
    exists = cfg.manifest_path.is_file()
    typer.echo(f"[infogrep] target: {cfg.target_dir}")
    typer.echo(f"[infogrep] indexed: {'yes' if exists else 'no'}")
    if not exists:
        typer.echo("[infogrep] run `infogrep index <dir>` to build the index.")


if __name__ == "__main__":  # pragma: no cover
    app()
