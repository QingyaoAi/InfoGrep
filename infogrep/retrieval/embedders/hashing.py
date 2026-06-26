"""Deterministic hashing embedder: no model, no downloads.

Useful for tests and as a zero-dependency fallback. It is a hashing bag-of-words with
L2 normalization, so exact lexical overlap produces high cosine similarity. It is *not*
semantic — real semantic retrieval uses the sentence-transformers embedder.
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

_TOKEN_RE = re.compile(r"\w+")


class HashEmbedder:
    name = "hash"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _embed_one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for tok in _TOKEN_RE.findall(text.lower()):
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:8], "little")
            vec[h % self.dim] += 1.0
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._embed_one(t) for t in texts])
