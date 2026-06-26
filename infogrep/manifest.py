"""SQLite manifest: source of truth for incremental indexing.

Tracks, per file, ``size``/``mtime``/``content_hash`` (for change detection) and the
passages produced from it. Passage text lives here too so M2/M3 can read it back when
building the sparse and dense indices.

Everything is disk-backed; nothing is held in memory beyond the current file's passages.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .ingest.types import Passage

SCHEMA_VERSION = 1


class Manifest:
    """Disk-backed record of what has been indexed for one directory."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # -- lifecycle ---------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS files (
                path            TEXT PRIMARY KEY,
                size            INTEGER NOT NULL,
                mtime           REAL    NOT NULL,
                content_hash    TEXT    NOT NULL,
                n_passages      INTEGER NOT NULL DEFAULT 0,
                indexed_version INTEGER NOT NULL DEFAULT 0,
                indexed_at      REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passages (
                passage_id TEXT PRIMARY KEY,
                doc_id     TEXT NOT NULL,
                path       TEXT NOT NULL,
                ordinal    INTEGER NOT NULL,
                page       INTEGER,
                offset     INTEGER NOT NULL,
                text       TEXT NOT NULL,
                FOREIGN KEY (path) REFERENCES files(path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_passages_doc ON passages(doc_id);
            """
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def next_version(self) -> int:
        current = int(self.get_meta("index_version", "0"))
        nxt = current + 1
        self.set_meta("index_version", str(nxt))
        return nxt

    # -- files -------------------------------------------------------------

    def get_file(self, path: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()

    def all_paths(self) -> set[str]:
        rows = self._conn.execute("SELECT path FROM files").fetchall()
        return {r["path"] for r in rows}

    def upsert_file(
        self,
        path: str,
        size: int,
        mtime: float,
        content_hash: str,
        n_passages: int,
        version: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO files(path, size, mtime, content_hash, n_passages,
                              indexed_version, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size            = excluded.size,
                mtime           = excluded.mtime,
                content_hash    = excluded.content_hash,
                n_passages      = excluded.n_passages,
                indexed_version = excluded.indexed_version,
                indexed_at      = excluded.indexed_at
            """,
            (path, size, mtime, content_hash, n_passages, version, time.time()),
        )

    def delete_file(self, path: str) -> None:
        """Remove a file and its passages (ON DELETE CASCADE handles passages)."""
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))

    def replace_passages(self, path: str, passages: list[Passage]) -> None:
        """Atomically swap the passages for one file."""
        self._conn.execute("DELETE FROM passages WHERE path = ?", (path,))
        self._conn.executemany(
            """
            INSERT INTO passages(passage_id, doc_id, path, ordinal, page, offset, text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (p.passage_id, p.doc_id, p.path, p.ordinal, p.page, p.offset, p.text)
                for p in passages
            ],
        )

    def commit(self) -> None:
        self._conn.commit()

    def iter_passages(self):
        """Stream every stored passage as a Row (memory-light; uses a server-side cursor)."""
        cur = self._conn.execute(
            "SELECT passage_id, doc_id, path, ordinal, page, offset, text "
            "FROM passages ORDER BY path, ordinal"
        )
        while True:
            rows = cur.fetchmany(500)
            if not rows:
                break
            yield from rows

    def count_passages(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS c FROM passages").fetchone()["c"]

    def get_passages_by_ids(self, passage_ids: list[str]) -> dict[str, sqlite3.Row]:
        """Look up passage rows by id (for enriching retriever hits with metadata)."""
        out: dict[str, sqlite3.Row] = {}
        for i in range(0, len(passage_ids), 500):
            batch = passage_ids[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                f"SELECT * FROM passages WHERE passage_id IN ({placeholders})", batch
            ):
                out[row["passage_id"]] = row
        return out

    # -- stats -------------------------------------------------------------

    def stats(self) -> dict:
        n_files = self._conn.execute("SELECT COUNT(*) AS c FROM files").fetchone()["c"]
        n_passages = self._conn.execute("SELECT COUNT(*) AS c FROM passages").fetchone()["c"]
        return {
            "n_files": n_files,
            "n_passages": n_passages,
            "index_version": int(self.get_meta("index_version", "0")),
            "last_indexed_at": self.get_meta("last_indexed_at"),
        }
