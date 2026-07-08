"""Daily reindex scheduling via macOS launchd (LaunchAgents).

Installs a per-directory LaunchAgent that runs ``infogrep index <dir>`` on a daily
calendar schedule. The agent uses absolute interpreter/PATH/HOME so it works outside
an interactive shell (needed for brew's JDK detection and the HF cache).

macOS only for now — ``launchd`` has no portable equivalent. On Linux, set up your own
cron job or systemd timer instead, e.g.:

    0 3 * * *  cd /path/to/InfoGrep && uv run infogrep index /path/to/dir
"""

from __future__ import annotations

import hashlib
import plistlib
import subprocess
import sys
from pathlib import Path

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
_LABEL_PREFIX = "com.infogrep.reindex"

NOT_SUPPORTED_MSG = (
    "`infogrep schedule` uses macOS launchd and isn't available on this platform.\n"
    "Set up your own cron job or systemd timer instead, e.g.:\n"
    "    0 3 * * *  cd /path/to/InfoGrep && uv run infogrep index /path/to/dir"
)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError(NOT_SUPPORTED_MSG)


def _label(directory: Path) -> str:
    digest = hashlib.sha1(str(directory).encode()).hexdigest()[:10]
    return f"{_LABEL_PREFIX}.{digest}"


def _plist_path(directory: Path) -> Path:
    return LAUNCH_AGENTS / f"{_label(directory)}.plist"


def _agent_env() -> dict[str, str]:
    """Minimal environment so launchd can find brew (JDK), the venv, and the HF cache."""
    venv_bin = str(Path(sys.executable).parent)
    return {
        "PATH": f"{venv_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
    }


def install(directory: Path, hour: int = 3, minute: int = 0) -> Path:
    """Install (or replace) a daily reindex agent for ``directory`` at HH:MM. Returns plist path."""
    _require_macos()
    directory = Path(directory).expanduser().resolve()
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    # Log into the (separate) index location, never the indexed folder.
    from .config import index_dir_for

    log = index_dir_for(directory) / "reindex.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": _label(directory),
        # Run via the current interpreter so no shell/PATH resolution is needed.
        "ProgramArguments": [sys.executable, "-m", "infogrep.cli", "index", str(directory)],
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "EnvironmentVariables": _agent_env(),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "RunAtLoad": False,
    }
    path = _plist_path(directory)
    with path.open("wb") as fh:
        plistlib.dump(plist, fh)

    # Reload: unload any prior version, then load the new one (ignore unload errors).
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(path)], capture_output=True, check=True)
    return path


def uninstall(directory: Path) -> bool:
    """Remove the daily reindex agent for ``directory``. Returns True if one existed."""
    directory = Path(directory).expanduser().resolve()
    path = _plist_path(directory)
    if not path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    path.unlink()
    return True


def list_agents() -> list[dict]:
    """List installed InfoGrep reindex agents (label, schedule, target dir)."""
    out: list[dict] = []
    if not LAUNCH_AGENTS.is_dir():
        return out
    for path in sorted(LAUNCH_AGENTS.glob(f"{_LABEL_PREFIX}.*.plist")):
        try:
            data = plistlib.loads(path.read_bytes())
        except Exception:
            continue
        args = data.get("ProgramArguments", [])
        cal = data.get("StartCalendarInterval", {})
        out.append(
            {
                "label": data.get("Label", path.stem),
                "directory": args[-1] if args else "?",
                "hour": cal.get("Hour", 0),
                "minute": cal.get("Minute", 0),
                "plist": str(path),
            }
        )
    return out
