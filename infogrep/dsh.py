"""Register InfoGrep's MCP server with DeepSeek Harness (``dsh``).

A dsh profile loads MCP servers from its patch layer,
``$DSH_HOME/profiles/<name>/cordis.patch.yml``: a top-level YAML list in which an
``insert`` entry names the stdio command to launch. This module adds, refreshes and
removes InfoGrep's entry there, so its tools appear in that profile's sessions as
``mcp__infogrep__<tool>`` — the same names Claude Code and Codex use.

The file belongs to the user, and to dsh's own settings pages, which rewrite it; it
may also hold ``!!js`` expressions that no YAML library here can round-trip. So it is
edited as text: InfoGrep's entry sits between two marker comments and nothing outside
them is touched. An ``infogrep`` server someone added by hand is left alone, because
dsh refuses to load a second entry with the same server name.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "infogrep"
ENTRY_ID = "mcp-infogrep"
DEFAULT_PROFILE = "web"  # what plain `dsh web` boots

_BEGIN = "# >>> InfoGrep MCP server (managed by `infogrep dsh`; edits in here are overwritten) >>>"
_END = "# <<< InfoGrep MCP server <<<"
_MANAGED_RE = re.compile(rf"^{re.escape(_BEGIN)}\n.*?^{re.escape(_END)}\n?", re.DOTALL | re.MULTILINE)
# An infogrep server that is already there, whoever wrote it (quotes optional in YAML).
_PRESENT_RE = re.compile(
    rf"""^\s*(?:-\s*)?(?:serverName:\s*['"]?{SERVER_NAME}['"]?|id:\s*['"]?{ENTRY_ID}['"]?)\s*(?:#.*)?$""",
    re.MULTILINE,
)


def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME", "~/.dsh")).expanduser()


def profiles() -> list[str]:
    """Names of the dsh profiles that exist on this machine."""
    root = dsh_home() / "profiles"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "cordis.yml").is_file())


def patch_path(profile: str) -> Path:
    return dsh_home() / "profiles" / profile / "cordis.patch.yml"


def infogrep_command() -> str:
    """Absolute path of the ``infogrep`` executable dsh should launch.

    dsh starts it outside any virtualenv, so a bare ``infogrep`` would only work for an
    install that happens to be on its PATH.
    """
    beside_python = Path(sys.executable).parent / "infogrep"
    if beside_python.is_file():
        return str(beside_python)
    return shutil.which("infogrep") or "infogrep"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"  # YAML single-quoted scalar


def _entry(command: str, directory: Path) -> str:
    return f"""{_BEGIN}
- insert:
    - id: {ENTRY_ID}
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: {SERVER_NAME}
        transport: stdio
        command: {_quote(command)}
        args: [ 'mcp', '--dir', {_quote(str(directory))} ]
        # Indexing a large folder runs far past the default 60 s allowed per tool call.
        toolCallTimeoutMs: 900000
{_END}
"""


def status(profile: str) -> str:
    """``"managed"`` (InfoGrep's own entry), ``"manual"`` (someone else's) or ``"absent"``."""
    path = patch_path(profile)
    text = path.read_text() if path.is_file() else ""
    if _MANAGED_RE.search(text):
        return "managed"
    return "manual" if _PRESENT_RE.search(text) else "absent"


def register(profile: str, directory: Path, command: str | None = None) -> str:
    """Add InfoGrep to a profile, or refresh the entry it added before.

    Returns ``"added"``, ``"updated"``, ``"unchanged"``, or ``"manual"`` when the
    profile already has an infogrep server this module did not write (left as is).
    Raises ``FileNotFoundError`` for a profile that does not exist and ``ValueError``
    when the patch file is not laid out in a way that can be extended safely; in both
    cases, and when dsh itself rejects the result, the file is left as it was.
    """
    path = patch_path(profile)
    if not path.parent.is_dir():
        raise FileNotFoundError(
            f"no dsh profile named '{profile}' under {dsh_home() / 'profiles'} "
            f"(run `dsh {profile}` once to create it)"
        )
    before = path.read_text() if path.is_file() else ""
    entry = _entry(command or infogrep_command(), Path(directory).expanduser().resolve())

    if _MANAGED_RE.search(before):
        after = _MANAGED_RE.sub(lambda _: entry, before, count=1)
        outcome = "unchanged" if after == before else "updated"
    elif _PRESENT_RE.search(before):
        return "manual"
    else:
        after, outcome = _append(before, entry), "added"

    if after != before:
        _write_checked(path, profile, before, after)
    return outcome


def unregister(profile: str) -> str:
    """Remove the entry :func:`register` added. Returns ``"removed"``, ``"absent"``,
    or ``"manual"`` for an infogrep server this module did not write (left as is)."""
    path = patch_path(profile)
    before = path.read_text() if path.is_file() else ""
    if not _MANAGED_RE.search(before):
        return "manual" if _PRESENT_RE.search(before) else "absent"
    after = _MANAGED_RE.sub("", before, count=1)
    if not _entries(after):  # the list must stay a list once its only entry is gone
        after = after.rstrip("\n") + "\n[]\n"
    _write_checked(path, profile, before, after)
    return "removed"


def _entries(text: str) -> list[str]:
    """The lines of a patch file that are YAML rather than comments or blanks."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _append(text: str, entry: str) -> str:
    body = _entries(text)
    if body == ["[]"]:  # the template dsh creates: comments, then an empty list
        kept = [ln for ln in text.splitlines() if ln.strip() != "[]"]
        return "\n".join(kept).rstrip("\n") + "\n\n" + entry
    if not body:
        return (text.rstrip("\n") + "\n\n" if text.strip() else "") + entry
    if body[0].startswith("- "):  # a block list: one more top-level item at the end
        return text.rstrip("\n") + "\n\n" + entry
    raise ValueError(
        "its entries are not a plain `- item` list, so nothing was changed. "
        "Add this to it by hand:\n\n" + entry
    )


def _write_checked(path: Path, profile: str, before: str, after: str) -> None:
    """Write the patch, then have dsh load it; put the old text back if it won't."""
    path.write_text(after)
    problem = _dsh_rejects(profile)
    if problem:
        if before:
            path.write_text(before)
        else:
            path.unlink()
        raise ValueError(f"dsh could not load the edited file, so it was restored: {problem}")


def _dsh_rejects(profile: str) -> str | None:
    """Why dsh fails to compose ``profile`` now, or None (also when dsh isn't installed)."""
    dsh = shutil.which("dsh")
    if dsh is None:
        return None
    try:
        proc = subprocess.run(
            [dsh, "--profile", profile, "--dump-config"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout).strip()[-400:] or f"exit {proc.returncode}"
    return None
