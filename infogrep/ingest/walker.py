"""Filesystem walker honoring include/exclude globs (gitignore-style)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from pathspec import PathSpec

from ..config import SIDECAR_DIRNAME, Config


def _spec(patterns: list[str]) -> PathSpec:
    """Compile patterns for case-insensitive matching (they are lowercased here).

    Candidate paths are lowercased to match, so a config listing ``**/*.png`` also picks
    up ``PHOTO.PNG``. Someone who writes an extension in lower case means the extension,
    not one spelling of it — and on macOS and Windows the filesystem itself does not
    distinguish them. Extraction already agrees: the registry keys off ``suffix.lower()``.
    """
    return PathSpec.from_lines("gitignore", [p.lower() for p in patterns])


def walk(config: Config) -> Iterator[tuple[Path, str]]:
    """Yield ``(absolute_path, relative_posix_path)`` for every file to index.

    A file is yielded when it matches any ``include`` pattern and no ``exclude``
    pattern. The side-car and ``.git`` directories are pruned during the walk for
    speed, regardless of patterns.
    """
    root = config.target_dir
    include = _spec(config.include)
    exclude = _spec(config.exclude)
    always_pruned = {SIDECAR_DIRNAME, ".git"}

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune directories in place so os.walk never descends into them: always skip
        # .git/.infogrep, and skip any directory matching an exclude pattern (so huge
        # trees like node_modules aren't traversed at all).
        kept = []
        for d in dirnames:
            if d in always_pruned:
                continue
            rel_d = (Path(dirpath) / d).relative_to(root).as_posix().lower()
            if exclude.match_file(rel_d) or exclude.match_file(rel_d + "/"):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            abs_path = Path(dirpath) / name
            rel = abs_path.relative_to(root).as_posix()
            key = rel.lower()  # patterns are lowercased in _spec; match case-insensitively
            if include.match_file(key) and not exclude.match_file(key):
                yield abs_path, rel
