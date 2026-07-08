"""Index orchestration: ingest -> chunk -> store, with incremental change detection.

M1 builds the manifest + passages. Writing the sparse (M2) and dense (M3) indices
hooks in here later; for now passages are persisted in the manifest, which those
backends will read from.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ChunkConfig, Config
from .ingest.chunker import chunk_pages
from .ingest.extract.registry import extract, is_supported
from .ingest.types import Passage
from .ingest.walker import walk
from .manifest import Manifest

_HASH_CHUNK = 1 << 20  # 1 MiB streaming read; never load whole files into memory
_COMMIT_EVERY = 500  # commit the manifest every N files: durable + resumable progress


def _take(iterator, n):
    """Pull up to ``n`` items from an iterator (for priming the worker pool)."""
    return list(itertools.islice(iterator, n))


def _extract_task(task):
    """Worker (runs in a separate process): extract + chunk + hash one file.

    Module-level and picklable so it can run under ProcessPoolExecutor. Returns
    ``(rel, passages, content_hash, error_or_None)``; passages is empty when the file
    has no extractable content (the parent then indexes it name-only).
    """
    abs_path, rel, ocr, ocr_min_chars, size, overlap, supported = task
    p = Path(abs_path)
    passages: list[Passage] = []
    err = None
    try:
        if supported:
            pages = extract(p, ocr=ocr, ocr_min_chars=ocr_min_chars)
            passages = chunk_pages(rel, pages, ChunkConfig(size=size, overlap=overlap))
    except Exception as exc:  # extractor failure shouldn't abort the run
        err = f"{rel}: {exc}"
    try:
        content_hash = _hash_file(p)
    except OSError as exc:
        content_hash, err = "", err or f"{rel}: {exc}"
    return rel, passages, content_hash, err


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
    name_only: int = 0  # indexed by file name/path only (no extractable content)
    n_files: int = 0
    n_passages: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Indexer:
    """Builds and incrementally updates a directory's side-car index."""

    def __init__(self, config: Config):
        self.config = config
        # Content hashes computed during change detection (dropped after each run).
        self._hash_cache: dict[Path, str] = {}

    def reindex(self, full: bool = False, on_progress=None) -> IndexReport:
        """(Re)index the directory. Only changed files are re-extracted (the manifest
        remembers what's indexed); extraction runs across worker processes; the manifest
        is committed periodically so an interrupted run resumes instead of restarting.
        """
        cfg = self.config
        cfg.index_dir.mkdir(parents=True, exist_ok=True)
        # Record which folder this index belongs to (the target is never written to).
        (cfg.index_dir / "source.txt").write_text(str(cfg.target_dir))
        report = IndexReport()

        with Manifest(cfg.manifest_path) as manifest:
            version = manifest.next_version()
            seen: set[str] = set()
            removed_ids: set[str] = set()  # passage-level delta for incremental backends
            changed_paths: set[str] = set()

            # 1) Walk + change-detect (cheap, single pass). Only added/modified files
            #    need (re)extraction; unchanged ones are skipped entirely.
            todo: list[tuple[Path, str, os.stat_result, str]] = []
            for abs_path, rel in walk(cfg):
                seen.add(rel)
                try:
                    stat = abs_path.stat()
                except OSError as exc:
                    report.errors.append(f"{rel}: {exc}")
                    continue
                change = self._classify(manifest.get_file(rel), stat, abs_path, full)
                if change == "unchanged":
                    report.unchanged += 1
                    continue
                if change == "modified":
                    removed_ids.update(manifest.passage_ids_for_path(rel))
                todo.append((abs_path, rel, stat, change))

            total = len(todo)
            done = 0

            def store(rel, passages, content_hash, err, stat, change):
                nonlocal done
                if err:
                    report.errors.append(err)
                if not passages:  # no content -> still indexed by name/path
                    passages = [self._stub_passage(rel)]
                    report.name_only += 1
                manifest.upsert_file(
                    path=rel, size=stat.st_size, mtime=stat.st_mtime,
                    content_hash=content_hash, n_passages=len(passages), version=version,
                )
                # New files: pure INSERT (no DELETE scan). Modified: delete-then-insert.
                if change == "added":
                    manifest.add_passages(passages)
                else:
                    manifest.replace_passages(rel, passages)
                changed_paths.add(rel)
                report.added += 1 if change == "added" else 0
                report.modified += 1 if change == "modified" else 0
                done += 1
                if done % _COMMIT_EVERY == 0:  # durable, resumable progress
                    manifest.commit()
                    manifest.checkpoint()  # keep the WAL bounded (don't let it grow to GBs)
                    if on_progress:
                        on_progress(done, total)

            def task_for(item):
                abs_path, rel, _stat, _change = item
                return (str(abs_path), rel, cfg.ingest.ocr, cfg.ingest.ocr_min_chars,
                        cfg.chunk.size, cfg.chunk.overlap, is_supported(abs_path))

            # 2) Extract in parallel (bounded in-flight so memory stays low), writing
            #    results to the manifest on this thread (SQLite is single-writer).
            workers = self._worker_count(total)
            if workers > 1:
                from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

                it = iter(todo)
                with ProcessPoolExecutor(max_workers=workers) as ex:
                    inflight = {}
                    for item in _take(it, workers * 2):
                        inflight[ex.submit(_extract_task, task_for(item))] = item
                    while inflight:
                        finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                        for fut in finished:
                            _, rel, stat, change = inflight.pop(fut)
                            _rel, passages, content_hash, err = fut.result()
                            store(rel, passages, content_hash, err, stat, change)
                            nxt = next(it, None)
                            if nxt is not None:
                                inflight[ex.submit(_extract_task, task_for(nxt))] = nxt
            else:
                for item in todo:
                    _, rel, stat, change = item
                    _rel, passages, content_hash, err = _extract_task(task_for(item))
                    store(rel, passages, content_hash, err, stat, change)

            # 3) Files in the manifest but no longer on disk are deleted.
            for rel in manifest.all_paths() - seen:
                removed_ids.update(manifest.passage_ids_for_path(rel))
                manifest.delete_file(rel)
                report.deleted += 1

            manifest.set_meta("last_indexed_at", str(time.time()))
            manifest.commit()
            if on_progress and total:
                on_progress(done, total)

            stats = manifest.stats()
            report.n_files = stats["n_files"]
            report.n_passages = stats["n_passages"]

            self._build_graph(manifest, report, full)
            self._build_backends(manifest, report, full, removed_ids, changed_paths)

        self._hash_cache.clear()
        return report

    def _worker_count(self, total: int) -> int:
        configured = self.config.ingest.workers
        if configured and configured > 0:
            return max(1, min(configured, total))
        if total < 8:  # small jobs: process-spawn overhead isn't worth it
            return 1
        return max(1, min(min(8, os.cpu_count() or 4), total))

    def status(self, check_staleness: bool = True) -> dict:
        if not self.config.manifest_path.is_file():
            return {"indexed": False}
        with Manifest(self.config.manifest_path) as manifest:
            stats = manifest.stats()
            stats["indexed"] = True
            if check_staleness:
                stats.update(self._staleness(manifest))
        self._hash_cache.clear()
        return stats

    def _staleness(self, manifest: Manifest) -> dict:
        """Count pending changes vs the filesystem without modifying the index."""
        cfg = self.config
        added = modified = 0
        seen: set[str] = set()
        for abs_path, rel in walk(cfg):
            if not is_supported(abs_path):
                continue
            seen.add(rel)
            try:
                stat = abs_path.stat()
            except OSError:
                continue
            change = self._classify(manifest.get_file(rel), stat, abs_path, full=False)
            if change == "added":
                added += 1
            elif change == "modified":
                modified += 1
        deleted = len(manifest.all_paths() - seen)
        pending = added + modified + deleted
        return {
            "pending": pending,
            "pending_added": added,
            "pending_modified": modified,
            "pending_deleted": deleted,
            "stale": pending > 0,
        }

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
        if abs_path not in self._hash_cache:
            self._hash_cache[abs_path] = _hash_file(abs_path)
        return self._hash_cache[abs_path]

    @staticmethod
    def _stub_passage(rel: str) -> Passage:
        """A passage for a file with no extractable text, so it's still searchable.

        Its content is the path tokens (e.g. "data archive zebra bin"): this keeps the
        file findable by name/path and avoids an empty ``contents`` field, which the
        Anserini batch indexer drops.
        """
        text = re.sub(r"[/\\._\-]+", " ", rel).strip()
        return Passage(
            passage_id=f"{rel}#0", doc_id=rel, path=rel, ordinal=0, text=text, page=None, offset=0
        )

    def _build_graph(self, manifest: Manifest, report: IndexReport, full: bool) -> None:
        """Rebuild the folder/filename metadata graph (see ``ingest.graph``).

        Metadata-only (paths and names, never content), so a full rebuild is cheap;
        only worth skipping when nothing structural changed since the last run.
        """
        cfg = self.config
        if not cfg.graph.enabled:
            return
        structural_change = report.added > 0 or report.deleted > 0
        if not (full or not cfg.graph_json_path.is_file() or structural_change):
            return
        try:
            from .ingest.graph import build_graph

            build_graph(cfg.index_dir, manifest.all_paths())
        except Exception as exc:  # never let a graph-build issue lose the manifest
            report.errors.append(f"graph: {exc}")

    def _build_backends(
        self,
        manifest: Manifest,
        report: IndexReport,
        full: bool,
        removed_ids: set[str],
        changed_paths: set[str],
    ) -> None:
        """Update retrieval indices from the manifest after a reindex.

        Dense applies the passage-level delta incrementally (delete removed ids, upsert
        changed passages) when a complete, embedder-matching index exists; otherwise it
        rebuilds. Sparse (Lucene/BM25) rebuilds on any change — it runs no model, so a
        full rebuild is sub-second, and true Lucene incremental would mean replicating
        Anserini's analyzer/field schema by hand (high risk, little gain).
        """
        cfg = self.config
        changed = report.added + report.modified + report.deleted > 0

        if cfg.sparse.enabled:
            from .retrieval.sparse import SparseIndex

            sparse = SparseIndex(
                cfg.sparse_dir, cfg.cache_dir,
                field_boosts=cfg.sparse.field_boosts, language=cfg.sparse.language,
            )
            # A committed Lucene index has a segments_N file; a crashed build won't.
            index_exists = cfg.sparse_dir.is_dir() and any(cfg.sparse_dir.glob("segments*"))
            # A language change requires re-tokenizing everything (full rebuild).
            lang_changed = index_exists and sparse.built_language() != cfg.sparse.language
            try:
                if full or not index_exists or lang_changed:
                    if changed or not index_exists or lang_changed:
                        sparse.build(manifest.iter_passages())
                elif removed_ids or changed_paths:
                    sparse.update(removed_ids, manifest.passages_for_paths(changed_paths))
            except Exception as exc:  # never let JVM/index issues lose the manifest
                report.errors.append(f"sparse index: {exc}")

        if cfg.dense.enabled:
            from .retrieval.dense import DenseIndex

            dense = DenseIndex(cfg)
            # A complete index is marked by embedder.json (a partial/OOM'd build lacks it).
            complete = (cfg.dense_dir / "embedder.json").is_file()
            # Incremental only if the existing index was built with the same embedder.
            embedder_matches = complete and dense.built_embedder_name() == dense.embedder.name
            try:
                if full or not complete or not embedder_matches:
                    if changed or not complete:
                        dense.build(manifest.iter_passages())
                elif removed_ids or changed_paths:
                    dense.update(removed_ids, manifest.passages_for_paths(changed_paths))
            except Exception as exc:  # embedding/Zvec issues shouldn't lose the manifest
                report.errors.append(f"dense index: {exc}")
