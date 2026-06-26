"""Embedder protocol shared by all dense models. (M3)"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Turns text into dense vectors. Implementations registered by key."""

    name: str
    dim: int

    def embed(self, texts: list[str]):  # -> np.ndarray  (M3)
        """Embed a batch of texts into an (len(texts), dim) array."""
        ...
