"""Embedder registry: build an embedder from DenseConfig.

Keys:
  - "qwen" / "st" / any other: SentenceTransformerEmbedder(config.model_name)
  - "hash": HashEmbedder (deterministic, no download; tests + fallback)
"""

from __future__ import annotations

from ...config import DenseConfig
from .base import Embedder


def get_embedder(config: DenseConfig) -> Embedder:
    if config.embedder == "hash":
        from .hashing import HashEmbedder

        return HashEmbedder()

    from .sentence_transformer import SentenceTransformerEmbedder

    return SentenceTransformerEmbedder(config.model_name, device=config.device)
