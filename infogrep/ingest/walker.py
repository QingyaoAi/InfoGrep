"""Filesystem walker honoring include/exclude globs (gitignore-style)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from pathspec import PathSpec

from ..config import Config, SIDECAR_DIRNAME


def _spec(patterns: list[str]) -> PathSpec:
    return PathSpec.from_lines("gitignore", patterns)


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
            rel_d = (Path(dirpath) / d).relative_to(root).as_posix()
            if exclude.match_file(rel_d) or exclude.match_file(rel_d + "/"):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            abs_path = Path(dirpath) / name
            rel = abs_path.relative_to(root).as_posix()
            if include.match_file(rel) and not exclude.match_file(rel):
                yield abs_path, rel
