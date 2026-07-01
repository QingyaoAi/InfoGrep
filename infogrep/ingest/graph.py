"""Metadata knowledge graph over folder/filename structure (no file content).

On every reindex, every currently-indexed file's *path* (never its content) contributes
to a folder tree. That tree is materialized two ways:

- an Obsidian-compatible vault of folder notes (linked by wikilinks) under the index's
  side-car ``graph_vault/`` directory — browsable in the Obsidian app if you open it there;
- a compact ``graph.json`` alongside it, which :mod:`infogrep.retrieval.graph` reads
  directly (no Obsidian app needed) to expand a hit's folder into sibling files during
  hybrid search.

Regenerated in full whenever files are added/removed — cheap, since it's pure path
manipulation with no extraction or embedding involved.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
ROOT = ""  # canonical key for the indexed directory's own top level
_ROOT_NOTE = "_root"


def _tokenize(name: str) -> list[str]:
    stem = PurePosixPath(name).stem
    stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)  # camelCase -> words
    return [t.lower() for t in _TOKEN_RE.findall(stem) if len(t) > 1]


def _parent_of(folder: str) -> str | None:
    if folder == ROOT:
        return None
    parent = str(PurePosixPath(folder).parent)
    return "" if parent == "." else parent


def _note_rel_path(folder: str) -> str:
    return f"{_ROOT_NOTE}.md" if folder == ROOT else f"{folder}.md"


def _link_target(folder: str) -> str:
    return _ROOT_NOTE if folder == ROOT else folder


def _sanitize_link(name: str) -> str:
    """Keep wikilink/list syntax intact even if a real file/folder name contains it."""
    return name.replace("[", "(").replace("]", ")")


def build_folder_tree(file_paths) -> dict[str, dict]:
    """Build the in-memory folder tree from relative file paths (metadata only)."""
    folders: dict[str, dict] = {}

    def ensure(folder: str) -> dict:
        node = folders.get(folder)
        if node is None:
            node = folders[folder] = {
                "files": [],
                "subfolders": set(),
                "parent": _parent_of(folder),
            }
            parent = node["parent"]
            if parent is not None:
                ensure(parent)["subfolders"].add(folder)
        return node

    ensure(ROOT)
    for rel in sorted(file_paths):
        pp = PurePosixPath(rel)
        parent_str = str(pp.parent)
        folder = "" if parent_str == "." else parent_str
        ensure(folder)["files"].append(pp.name)
    return folders


def build_graph(index_dir: Path, file_paths) -> None:
    """(Re)build the folder-metadata graph: an Obsidian vault + a fast JSON index."""
    index_dir = Path(index_dir)
    folders = build_folder_tree(file_paths)

    _write_vault(index_dir / "graph_vault", folders)

    graph_json = {
        folder: {
            "parent": node["parent"],
            "subfolders": sorted(node["subfolders"]),
            "files": sorted(node["files"]),
            "name_tokens": _tokenize(PurePosixPath(folder).name) if folder else [],
            "file_tokens": sorted({t for f in node["files"] for t in _tokenize(f)}),
        }
        for folder, node in folders.items()
    }
    (index_dir / "graph.json").write_text(json.dumps(graph_json))


def _write_vault(vault_dir: Path, folders: dict[str, dict]) -> None:
    # Full regen: folders can be renamed/removed between runs, so stale notes (and the
    # now-empty directories they left behind) need clearing, not just overwriting.
    shutil.rmtree(vault_dir, ignore_errors=True)
    vault_dir.mkdir(parents=True, exist_ok=True)

    for folder, node in folders.items():
        note_path = vault_dir / _note_rel_path(folder)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(_render_note(folder, node))


def _render_note(folder: str, node: dict) -> str:
    label = folder if folder else "/ (indexed root)"
    lines = [
        "---",
        "type: folder",
        f'path: "{folder}"',
        f"files: {len(node['files'])}",
        f"subfolders: {len(node['subfolders'])}",
        "---",
        "",
        f"# {label}",
        "",
    ]
    parent = node["parent"]
    if parent is not None:
        lines += ["## Parent", f"- [[{_sanitize_link(_link_target(parent))}]]", ""]
    if node["subfolders"]:
        lines.append("## Subfolders")
        lines += [
            f"- [[{_sanitize_link(_link_target(sub))}]]" for sub in sorted(node["subfolders"])
        ]
        lines.append("")
    if node["files"]:
        lines.append("## Files")
        lines += [f"- {_sanitize_link(name)}" for name in sorted(node["files"])]
        lines.append("")
    return "\n".join(lines)
