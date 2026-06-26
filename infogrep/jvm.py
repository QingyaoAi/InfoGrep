"""Locate a suitable JDK and export ``JAVA_HOME`` before Pyserini starts the JVM.

Pyserini (Anserini/Lucene) needs JDK 21+. macOS often defaults to an older JDK, so we
detect a compatible one and set ``JAVA_HOME`` in-process. This must run *before* anything
imports ``pyserini``/``jnius`` (the JVM boots at import time).
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

MIN_JDK = 21


def _release_version(java_home: Path) -> int | None:
    """Read the major version from a JDK's ``release`` file (no JVM start needed)."""
    release = java_home / "release"
    if not release.is_file():
        return None
    m = re.search(r'JAVA_VERSION="?(\d+)', release.read_text(errors="ignore"))
    return int(m.group(1)) if m else None


def _candidates() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("JAVA_HOME")
    if env:
        paths.append(Path(env))

    # Homebrew keg (Apple Silicon + Intel layouts).
    try:
        prefix = subprocess.run(
            ["brew", "--prefix", f"openjdk@{MIN_JDK}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if prefix:
            paths.append(Path(prefix) / "libexec" / "openjdk.jdk" / "Contents" / "Home")
            paths.append(Path(prefix))
    except (OSError, subprocess.SubprocessError):
        pass

    # macOS JavaVirtualMachines + java_home helper.
    try:
        jh = subprocess.run(
            ["/usr/libexec/java_home", "-v", str(MIN_JDK)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if jh:
            paths.append(Path(jh))
    except (OSError, subprocess.SubprocessError):
        pass

    for base in ("/opt/homebrew/opt", "/usr/local/opt"):
        paths.append(Path(base) / f"openjdk@{MIN_JDK}" / "libexec" / "openjdk.jdk" / "Contents" / "Home")
    return paths


@lru_cache(maxsize=1)
def ensure_jdk() -> str:
    """Find a JDK >= MIN_JDK, set ``JAVA_HOME``/``PATH``, and return its home path."""
    for home in _candidates():
        if home and home.is_dir() and (_release_version(home) or 0) >= MIN_JDK:
            os.environ["JAVA_HOME"] = str(home)
            bin_dir = str(home / "bin")
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return str(home)

    raise RuntimeError(
        f"InfoGrep's sparse backend (Pyserini) needs JDK {MIN_JDK}+, but none was found.\n"
        f"On macOS:  brew install openjdk@{MIN_JDK}\n"
        f"Then re-run, or set JAVA_HOME to a JDK {MIN_JDK}+ install."
    )
