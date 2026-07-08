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

    try:
        from .sentence_transformer import SentenceTransformerEmbedder
    except ImportError as exc:
        from ..dense import DENSE_EXTRA_HINT

        raise RuntimeError(DENSE_EXTRA_HINT) from exc

    return SentenceTransformerEmbedder(config.model_name, device=config.device)
