"""Index orchestration: ingest -> chunk -> store, with incremental change detection.

M1 builds the manifest + passages. Writing the sparse (M2) and dense (M3) indices
hooks in here later; for now passages are persisted in the manifest, which those
backends will read from.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .ingest.chunker import chunk_pages
from .ingest.extract.registry import extract, is_supported
from .ingest.walker import walk
from .manifest import Manifest

_HASH_CHUNK = 1 << 20  # 1 MiB streaming read; never load whole files into memory


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class IndexReport:
    """Summary of one (re)index run."""

    added: int = 0
    modified: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped: int = 0  # unsupported file types
    n_files: int = 0
    n_passages: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Indexer:
    """Builds and incrementally updates a directory's side-car index."""

    def __init__(self, config: Config):
        self.config = config

    def reindex(self, full: bool = False) -> IndexReport:
        cfg = self.config
        cfg.sidecar_dir.mkdir(parents=True, exist_ok=True)
        report = IndexReport()

        with Manifest(cfg.manifest_path) as manifest:
            version = manifest.next_version()
            seen: set[str] = set()

            for abs_path, rel in walk(cfg):
                seen.add(rel)
                if not is_supported(abs_path):
                    report.skipped += 1
                    continue

                try:
                    stat = abs_path.stat()
                except OSError as exc:
                    report.errors.append(f"{rel}: {exc}")
                    continue

                row = manifest.get_file(rel)
                change = self._classify(row, stat, abs_path, full)
                if change == "unchanged":
                    report.unchanged += 1
                    continue

                try:
                    passages = self._build_passages(abs_path, rel)
                except Exception as exc:  # extractor failure shouldn't abort the run
                    report.errors.append(f"{rel}: {exc}")
                    continue

                content_hash = self._cached_hash(abs_path)
                # File row must exist before passages (passages.path FK -> files.path).
                manifest.upsert_file(
                    path=rel,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    content_hash=content_hash,
                    n_passages=len(passages),
                    version=version,
                )
                manifest.replace_passages(rel, passages)
                if change == "added":
                    report.added += 1
                else:
                    report.modified += 1

            # Files in the manifest but no longer on disk are deleted.
            for rel in manifest.all_paths() - seen:
                manifest.delete_file(rel)
                report.deleted += 1

            manifest.set_meta("last_indexed_at", str(time.time()))
            manifest.commit()

            stats = manifest.stats()
            report.n_files = stats["n_files"]
            report.n_passages = stats["n_passages"]

            self._build_backends(manifest, report)

        # reset per-run hash cache
        self._hash_cache: dict[Path, str] = {}
        return report

    def status(self) -> dict:
        if not self.config.manifest_path.is_file():
            return {"indexed": False}
        with Manifest(self.config.manifest_path) as manifest:
            stats = manifest.stats()
        stats["indexed"] = True
        return stats

    # -- internals ---------------------------------------------------------

    def _classify(self, row, stat, abs_path: Path, full: bool) -> str:
        """Return 'added' | 'modified' | 'unchanged' for a file."""
        if row is None:
            return "added"
        if full:
            return "modified"
        # Cheap path: same size + mtime -> assume unchanged, skip hashing.
        if row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
            return "unchanged"
        # Stat differs; confirm with content hash (handles touch-without-edit).
        if self._cached_hash(abs_path) == row["content_hash"]:
            return "unchanged"
        return "modified"

    def _cached_hash(self, abs_path: Path) -> str:
        cache = getattr(self, "_hash_cache", None)
        if cache is None:
            cache = self._hash_cache = {}
        if abs_path not in cache:
            cache[abs_path] = _hash_file(abs_path)
        return cache[abs_path]

    def _build_passages(self, abs_path: Path, rel: str):
        pages = extract(abs_path)
        return chunk_pages(rel, pages, self.config.chunk)

    def _build_backends(self, manifest: Manifest, report: IndexReport) -> None:
        """Rebuild retrieval indices from the manifest when passages changed."""
        cfg = self.config
        changed = report.added + report.modified + report.deleted > 0

        if cfg.sparse.enabled:
            from .retrieval.sparse import SparseIndex

            sparse = SparseIndex(cfg.sparse_dir, cfg.cache_dir)
            index_exists = cfg.sparse_dir.is_dir() and any(cfg.sparse_dir.glob("segments*"))
            if changed or not index_exists:
                try:
                    sparse.build(manifest.iter_passages())
                except Exception as exc:  # never let JVM/index issues lose the manifest
                    report.errors.append(f"sparse index: {exc}")
