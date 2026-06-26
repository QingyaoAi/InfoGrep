"""Disk-backed embedding cache keyed by (model, passage text).

Lets re-index runs skip re-embedding passages whose text is unchanged, even across a
full dense rebuild. Vectors are stored as raw float32 bytes in SQLite.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import numpy as np


class EmbeddingCache:
    def __init__(self, db_path: Path, model_id: str):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_id = model_id
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS emb (key TEXT PRIMARY KEY, dim INTEGER, vec BLOB)"
        )
        self._conn.commit()

    def key(self, text: str) -> str:
        h = hashlib.sha1()
        h.update(self.model_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def get_many(self, keys: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        # Chunk the IN clause to stay under SQLite's variable limit.
        for i in range(0, len(keys), 500):
            batch = keys[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            for row in self._conn.execute(
                f"SELECT key, vec FROM emb WHERE key IN ({placeholders})", batch
            ):
                out[row[0]] = np.frombuffer(row[1], dtype=np.float32)
        return out

    def put_many(self, items: list[tuple[str, np.ndarray]]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO emb(key, dim, vec) VALUES (?, ?, ?)",
            [(k, int(v.shape[0]), v.astype(np.float32).tobytes()) for k, v in items],
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
