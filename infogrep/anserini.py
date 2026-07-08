"""Anserini/Lucene JVM bootstrap — no pyserini Python package required.

The sparse backend talks to Anserini/Lucene *Java* classes directly through jnius;
the only thing it needs from Python is the Anserini fat jar on the JVM classpath.
Depending on the ``pyserini`` package for that pulled in torch, transformers, fastapi,
matplotlib, … (>1 GB) that InfoGrep never imports. Instead this module resolves the
jar itself, in order:

1. ``INFOGREP_ANSERINI_JAR`` env var (explicit override),
2. the jar bundled inside an installed ``pyserini`` package, if present,
3. a cached copy under ``$INFOGREP_HOME/jars/``, downloaded once from Maven Central
   (checksum-verified).

Import this module (not ``jnius``) to use the JVM: importing it locates a JDK
(:func:`infogrep.jvm.ensure_jdk`), configures the classpath, and boots the JVM —
so, like ``pyserini.pyclass``, it must be imported before anything else touches jnius.
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .config import index_home
from .jvm import ensure_jdk

ANSERINI_VERSION = "2.2.0"
_JAR_NAME = f"anserini-{ANSERINI_VERSION}-fatjar.jar"
_MAVEN_URL = f"https://repo1.maven.org/maven2/io/anserini/anserini/{ANSERINI_VERSION}/{_JAR_NAME}"
_JAR_SHA1 = "15e149c8cfaf7854e180ca5ebf82adadfbaca9c2"

# Quiet JUL/Lucene startup chatter (same settings pyserini ships).
_LOGGING_PROPERTIES = """\
handlers=java.util.logging.ConsoleHandler
.level=WARNING
java.util.logging.ConsoleHandler.level=WARNING
org.apache.lucene.internal.vectorization.PanamaVectorizationProvider.level=WARNING
"""


def _pyserini_jar() -> Path | None:
    """The fat jar bundled inside an installed pyserini package (same artifact)."""
    import importlib.util

    spec = importlib.util.find_spec("pyserini")  # cheap: does not import pyserini
    if spec is None or not spec.submodule_search_locations:
        return None
    jars = Path(spec.submodule_search_locations[0]) / "resources" / "jars"
    found = sorted(jars.glob("anserini-*-fatjar.jar"))
    return found[-1] if found else None


def find_local_jar() -> Path | None:
    """Locate an Anserini fat jar without downloading anything."""
    env = os.environ.get("INFOGREP_ANSERINI_JAR")
    if env and Path(env).is_file():
        return Path(env)
    bundled = _pyserini_jar()
    if bundled is not None:
        return bundled
    cached = index_home() / "jars" / _JAR_NAME
    return cached if cached.is_file() else None


def _download_jar() -> Path:
    """Fetch the fat jar from Maven Central into ``$INFOGREP_HOME/jars`` (once)."""
    dest = index_home() / "jars" / _JAR_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".jar.part")
    print(
        f"[infogrep] downloading Anserini {ANSERINI_VERSION} (~112 MB, one-time) "
        f"to {dest} …",
        file=sys.stderr, flush=True,
    )
    sha1 = hashlib.sha1()
    with urllib.request.urlopen(_MAVEN_URL, timeout=60) as resp, tmp.open("wb") as fh:
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            sha1.update(block)
            fh.write(block)
    if sha1.hexdigest() != _JAR_SHA1:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"Anserini jar checksum mismatch downloading {_MAVEN_URL}; "
            "try again, or place a jar manually and set INFOGREP_ANSERINI_JAR."
        )
    tmp.replace(dest)
    print("[infogrep] Anserini jar ready.", file=sys.stderr, flush=True)
    return dest


def jar_path() -> Path:
    """The Anserini fat jar to put on the classpath, downloading it if needed."""
    return find_local_jar() or _download_jar()


def _logging_properties_path() -> Path:
    path = index_home() / "jars" / "logging.properties"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_LOGGING_PROPERTIES)
    return path


def available() -> bool:
    """True when sparse search can run right now without a download (JDK + local jar)."""
    try:
        ensure_jdk()
    except RuntimeError:
        return False
    return find_local_jar() is not None


@contextmanager
def _suppress_jvm_startup_stderr():
    """Hide the JVM's fd-level startup warnings (bypass any Python-level redirect)."""
    if os.environ.get("INFOGREP_VERBOSE_JVM"):
        yield
        return
    sys.stderr.flush()
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


# -- boot the JVM at import time (mirrors pyserini.pyclass) ----------------------------

ensure_jdk()

import jnius_config  # noqa: E402

if not jnius_config.vm_running:
    jnius_config.add_classpath(str(jar_path()))
    jnius_config.add_options("--add-modules=jdk.incubator.vector")
    # Suppress "WARNING: A restricted method in java.lang.foreign.Linker has been called"
    jnius_config.add_options("--enable-native-access=ALL-UNNAMED")
    if not os.environ.get("INFOGREP_VERBOSE_JVM"):
        jnius_config.add_options(
            f"-Djava.util.logging.config.file={_logging_properties_path()}"
        )
        jnius_config.add_options("-Dslf4j.internal.verbosity=WARN")

with _suppress_jvm_startup_stderr():
    from jnius import autoclass, cast  # noqa: E402

__all__ = ["autoclass", "cast", "available", "jar_path", "ANSERINI_VERSION"]
