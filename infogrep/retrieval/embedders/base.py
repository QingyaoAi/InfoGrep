"""Embedder protocol shared by all dense models. (M3)"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Turns text into dense vectors. Implementations registered by key."""

    name: str
    dim: int

    def embed(self, texts: list[str], is_query: bool = False):  # -> np.ndarray
        """Embed a batch of texts into an (len(texts), dim) float32 array.

        ``is_query`` lets instruction-tuned models (e.g. Qwen3-Embedding) apply a
        query-side prompt; document-style embedders can ignore it.
        """
        ...
