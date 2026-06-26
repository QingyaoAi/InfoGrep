"""SQLite manifest: tracks files, passages, and index versions for incremental updates.

Implemented in M1. The manifest is the source of truth for change detection
(``path -> size/mtime/content_hash``) and the doc/passage mapping.
"""

from __future__ import annotations

from pathlib import Path


class Manifest:
    """Disk-backed record of what has been indexed. (M1)"""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def __getattr__(self, name: str):  # pragma: no cover - scaffold guard
        raise NotImplementedError("Manifest lands in M1 (ingestion + change detection).")
