"""Locate a suitable JDK and export ``JAVA_HOME`` before Pyserini starts the JVM.

Anserini/Lucene needs JDK 21+. The system default `java` is often older (or absent), so
we detect a compatible install and set ``JAVA_HOME`` in-process. This must run *before*
anything imports ``jnius`` (the JVM boots at import time). Covers both
macOS (Homebrew, ``java_home``) and Linux (``/usr/lib/jvm``, ``update-alternatives``,
Linuxbrew).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

MIN_JDK = 21
LINUX_JVM_DIR = Path("/usr/lib/jvm")


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

    # Homebrew keg (macOS Apple Silicon/Intel, and Linuxbrew).
    try:
        prefix = subprocess.run(
            ["brew", "--prefix", f"openjdk@{MIN_JDK}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if prefix:
            paths.append(Path(prefix) / "libexec" / "openjdk.jdk" / "Contents" / "Home")  # macOS
            paths.append(Path(prefix))  # Linux keg layout (JAVA_HOME == prefix)
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

    # Linux: distro packages under /usr/lib/jvm (Debian/Ubuntu, Fedora, Arch, ...).
    if LINUX_JVM_DIR.is_dir():
        paths.extend(sorted(LINUX_JVM_DIR.iterdir(), reverse=True))

    # Linux: update-alternatives' chosen `java`, and whatever `java` resolves to on PATH
    # (each is a bin/java symlink two levels below JAVA_HOME).
    try:
        alt = subprocess.run(
            ["update-alternatives", "--list", "java"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        for line in alt.splitlines():
            if line:
                paths.append(Path(line).resolve().parent.parent)
    except (OSError, subprocess.SubprocessError):
        pass

    which_java = shutil.which("java")
    if which_java:
        paths.append(Path(which_java).resolve().parent.parent)

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

    if sys.platform == "darwin":
        install_hint = f"On macOS:  brew install openjdk@{MIN_JDK}"
    else:
        install_hint = (
            f"On Debian/Ubuntu:  sudo apt install openjdk-{MIN_JDK}-jdk\n"
            f"On Fedora/RHEL:    sudo dnf install java-{MIN_JDK}-openjdk\n"
            f"On Arch:           sudo pacman -S jdk-openjdk"
        )
    raise RuntimeError(
        f"InfoGrep's sparse backend (Pyserini) needs JDK {MIN_JDK}+, but none was found.\n"
        f"{install_hint}\n"
        f"Then re-run, or set JAVA_HOME to a JDK {MIN_JDK}+ install."
    )
