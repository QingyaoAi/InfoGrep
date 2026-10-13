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
from .engine import MODES

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

    typer.echo(f"[infogrep] indexing {cfg.target_dir}")
    typer.echo(f"[infogrep] index location: {cfg.index_dir}")

    def _progress(done: int, total: int) -> None:
        typer.echo(f"[infogrep] extracted {done}/{total} files…", err=True)

    report = Indexer(cfg).reindex(full=full, on_progress=_progress)
    typer.echo(
        "[infogrep] "
        f"added={report.added} modified={report.modified} deleted={report.deleted} "
        f"unchanged={report.unchanged} name_only={report.name_only}"
    )
    typer.echo(f"[infogrep] index now holds {report.n_files} files, {report.n_passages} passages")
    compacted = [
        name
        for name, done in (("sparse", report.compacted_sparse),
                           ("manifest", report.compacted_manifest))
        if done
    ]
    if compacted:
        typer.echo(f"[infogrep] reclaimed space from deleted files ({', '.join(compacted)})")
    for err in report.errors:
        typer.echo(f"[infogrep] error: {err}", err=True)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query."),
    directory: Path = typer.Option(Path.cwd(), "--dir", "-d", help="Indexed directory."),
    k: int = typer.Option(10, "--k", help="Number of results."),
    mode: str = typer.Option("hybrid", "--mode", "-m", help=" | ".join(MODES)),
    prf: bool = typer.Option(False, "--prf", help="RM3 pseudo-relevance feedback (sparse)."),
) -> None:
    """Query indexed content."""
    from .engine import SearchEngine

    engine = SearchEngine(Config.load(directory))

    try:
        out = engine.search(mode, query, k=k, prf=prf)
    except ValueError:
        typer.echo(f"[infogrep] unknown mode: {mode}", err=True)
        raise typer.Exit(code=2)
    except FileNotFoundError as exc:
        typer.echo(f"[infogrep] {exc}", err=True)
        raise typer.Exit(code=2)
    except RuntimeError as exc:  # e.g. missing JDK or optional 'dense' extra
        typer.echo(f"[infogrep] {exc}", err=True)
        raise typer.Exit(code=1)

    results = out.results
    if mode == "hybrid":
        if out.used:
            typer.echo(f"[infogrep] fused: {', '.join(out.used)}")
        for name, reason in out.skipped.items():
            typer.echo(f"[infogrep] skipped {name}: {reason}")

    if not results:
        typer.echo("[infogrep] no results.")
        return
    for i, r in enumerate(results, start=1):
        file_ref = r.abs_path or r.path  # original file path when known
        loc = file_ref + (f" p.{r.page}" if r.page is not None else "")
        typer.echo(f"{i:2}. [{r.score:.3f}] {loc}  ({r.retriever})")
        typer.echo(f"    {r.snippet.strip()[:160]}")


@app.command()
def learn(
    query: str = typer.Argument(..., help="Topic to research and persist in the KB."),
    directory: Path = typer.Option(Path.cwd(), "--dir", "-d", help="Indexed directory."),
    k: int = typer.Option(12, "--k", help="Passages to gather as sources."),
    max_entities: int = typer.Option(8, "--max-entities", help="Entity notes to link."),
) -> None:
    """Search indexed files for a topic and write linked notes into the KB vault."""
    from .engine import SearchEngine

    engine = SearchEngine(Config.load(directory))
    try:
        out = engine.learn(query, k=k, max_entities=max_entities)
    except (FileNotFoundError, ValueError) as exc:  # missing index / Obsidian down
        typer.echo(f"[infogrep] {exc}", err=True)
        raise typer.Exit(code=2)

    for name, reason in out["retrievers_skipped"].items():
        typer.echo(f"[infogrep] skipped {name}: {reason}")
    if not out["topic_note"]:
        typer.echo("[infogrep] no results for that query — nothing written.")
        raise typer.Exit(code=1)
    typer.echo(
        f"[infogrep] gathered {out['n_passages']} passages from {out['n_sources']} files "
        f"({', '.join(out['retrievers_used'])})"
    )
    typer.echo(f"[infogrep] topic note: {out['topic_note']}")
    for label in ("created", "updated", "unchanged"):
        for path in out[f"entity_notes_{label}"]:
            typer.echo(f"[infogrep] entity {label}: {path}")
    if not out["kb_search_enabled"]:
        typer.echo(
            "[infogrep] note: kb search is disabled — set [kb] enabled = true in this "
            "directory's config so searches can use the knowledge base."
        )


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question to answer from the indexed files."),
    directory: Path | None = typer.Option(
        None, "--dir", "-d", help="Indexed directory to consult (default: every indexed folder)."
    ),
    follow_up: bool = typer.Option(
        False, "--continue", "-c", help="Follow up on the previous question."
    ),
    notes: bool = typer.Option(
        False, "--notes", help="Also search the Obsidian knowledge base (slower)."
    ),
) -> None:
    """Answer a question from the indexed files with a DeepSeek Harness (dsh) agent.

    The agent searches and reads the files, then answers; it runs for this one
    question and cannot change anything. Progress goes to stderr, the answer to stdout.
    """
    from . import ask as asking

    session = asking.latest_session() if follow_up else None
    if follow_up and session is None:
        typer.echo("[infogrep] no earlier question to continue — asking afresh.", err=True)
    try:
        run = asking.Ask(question, directory=directory, session=session, notes=notes)
        for event in run.events():
            if event["type"] == "step":
                typer.echo(f"[infogrep] {event['text']}", err=True)
            elif event["type"] == "answer":
                typer.echo(event["text"])
                if event["sources"]:
                    typer.echo("\nSources:")
                    for path in event["sources"]:
                        typer.echo(f"  {path}")
    except asking.AskError as exc:
        typer.echo(f"[infogrep] {exc}", err=True)
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
    typer.echo(f"[infogrep] index location: {cfg.index_dir}")
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
    directory: Path | None = typer.Option(
        None, "--dir", "-d",
        help="Default indexed directory (default: $INFOGREP_DIR, else the current directory).",
    ),
    all_folders: bool = typer.Option(
        False, "--all-folders", help="Search every indexed folder unless a tool call names one."
    ),
    read_only: bool = typer.Option(
        False, "--read-only", help="Leave out the tools that write (reindex, kb_learn)."
    ),
) -> None:
    """Run the MCP server (stdio) so coding agents can call InfoGrep's search tools."""
    from .mcp_server import main as serve

    serve(
        directory=str(Path(directory).expanduser().resolve()) if directory else None,
        all_folders=all_folders,
        read_only=read_only,
    )


@app.command()
def serve(
    directory: Path = typer.Option(Path.cwd(), "--dir", "-d", help="Indexed directory to search."),
    port: int = typer.Option(7421, "--port", "-p", help="Port (uncommon by default)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host (localhost by default)."),
    exit_with_parent: bool = typer.Option(
        False, "--exit-with-parent", hidden=True,
        help="Exit when the launching process dies (used by the macOS app bundle).",
    ),
) -> None:
    """Run a local web UI to test search in a browser."""
    from .web import serve as run_web

    run_web(directory=directory, host=host, port=port, exit_with_parent=exit_with_parent)


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
    try:
        path = scheduler.install(directory, hour=hour, minute=minute)
    except RuntimeError as exc:
        typer.echo(f"[infogrep] {exc}", err=True)
        raise typer.Exit(code=1)
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


dsh_app = typer.Typer(help="Offer InfoGrep's search tools to DeepSeek Harness (dsh) sessions.")
app.add_typer(dsh_app, name="dsh")


def _dsh_profiles(chosen: list[str] | None) -> list[str]:
    from . import dsh

    return chosen or [dsh.DEFAULT_PROFILE]


@dsh_app.command("install")
def dsh_install(
    directory: Path = typer.Option(..., "--dir", "-d", help="Indexed directory to search by default."),
    profile: list[str] = typer.Option(
        None, "--profile", "-p", help="dsh profile to add InfoGrep to (repeatable). Default: web."
    ),
) -> None:
    """Register the InfoGrep MCP server in a dsh profile (tools: mcp__infogrep__…)."""
    from . import dsh

    failed = False
    for name in _dsh_profiles(profile):
        try:
            outcome = dsh.register(name, directory)
        except (FileNotFoundError, ValueError) as exc:
            typer.echo(f"[infogrep] dsh profile '{name}': {exc}", err=True)
            failed = True
            continue
        typer.echo(
            f"[infogrep] dsh profile '{name}': "
            + {
                "added": "InfoGrep added; its tools appear in new sessions",
                "updated": "InfoGrep entry updated",
                "unchanged": "InfoGrep already registered",
                "manual": "already has an infogrep server that was added by hand; left as is",
            }[outcome]
            + f"  ({dsh.patch_path(name)})"
        )
    if failed:
        raise typer.Exit(code=1)


@dsh_app.command("uninstall")
def dsh_uninstall(
    profile: list[str] = typer.Option(
        None, "--profile", "-p", help="dsh profile to remove InfoGrep from (repeatable). Default: all."
    ),
) -> None:
    """Remove the InfoGrep MCP server that `infogrep dsh install` registered."""
    from . import dsh

    for name in profile or dsh.profiles():
        try:
            outcome = dsh.unregister(name)
        except ValueError as exc:
            typer.echo(f"[infogrep] dsh profile '{name}': {exc}", err=True)
            raise typer.Exit(code=1)
        if outcome == "removed":
            typer.echo(f"[infogrep] dsh profile '{name}': InfoGrep removed")
        elif outcome == "manual":
            typer.echo(
                f"[infogrep] dsh profile '{name}': its infogrep server was added by hand; "
                f"remove it from {dsh.patch_path(name)} yourself"
            )


@dsh_app.command("list")
def dsh_list() -> None:
    """Show which dsh profiles have InfoGrep."""
    from . import dsh

    names = dsh.profiles()
    if not names:
        typer.echo(f"[infogrep] no dsh profiles under {dsh.dsh_home()} (is dsh installed?)")
        return
    label = {"managed": "yes", "manual": "yes (added by hand)", "absent": "no"}
    for name in names:
        typer.echo(f"[infogrep] {name:24s} {label[dsh.status(name)]}")


if __name__ == "__main__":  # pragma: no cover
    app()
