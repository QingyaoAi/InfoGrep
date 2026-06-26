"""Sentence-Transformers embedder (default: Qwen3-Embedding-0.6B).

Lazily loads the model so importing this module is cheap; the (large) model download
and load happen only when the first embedding is requested. Runs on MPS/CUDA/CPU.
"""

from __future__ import annotations

import numpy as np


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


class SentenceTransformerEmbedder:
    """Wraps a sentence-transformers model; normalized embeddings for cosine search."""

    def __init__(self, model_name: str, device: str = "auto", query_prompt_name: str = "query"):
        self.name = model_name
        self.model_name = model_name
        self.device = _resolve_device(device)
        self.query_prompt_name = query_prompt_name
        self._model = None
        self._dim: int | None = None

    def _load_model(self):
        import os

        from sentence_transformers import SentenceTransformer

        try:
            return SentenceTransformer(self.model_name, device=self.device)
        except Exception:
            # Transient hub error (e.g. 503) with a cached model: retry offline.
            os.environ["HF_HUB_OFFLINE"] = "1"
            return SentenceTransformer(self.model_name, device=self.device)

    @property
    def model(self):
        if self._model is None:
            self._model = self._load_model()
            # Method was renamed across sentence-transformers versions.
            get_dim = getattr(
                self._model, "get_embedding_dimension", None
            ) or self._model.get_sentence_embedding_dimension
            self._dim = get_dim()
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            _ = self.model  # triggers load and sets _dim
        return int(self._dim)

    def _encode(self, texts: list[str], is_query: bool, batch_size: int) -> np.ndarray:
        kwargs = dict(
            batch_size=batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        # Use the model's query prompt if it advertises one (Qwen3 does).
        if is_query:
            prompts = getattr(self.model, "prompts", None) or {}
            if self.query_prompt_name in prompts:
                kwargs["prompt_name"] = self.query_prompt_name
        return self.model.encode(texts, **kwargs).astype(np.float32)

    @staticmethod
    def _is_oom(exc: Exception) -> bool:
        return "out of memory" in str(exc).lower()

    def _free_memory(self) -> None:
        try:
            import torch

            if self.device == "mps":
                torch.mps.empty_cache()
            elif self.device == "cuda":
                torch.cuda.empty_cache()
        except Exception:
            pass

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        try:
            return self._encode(texts, is_query, batch_size=32)
        except RuntimeError as exc:
            if not self._is_oom(exc):
                raise
            # GPU OOM: free the cache and retry with a smaller batch.
            self._free_memory()
            try:
                return self._encode(texts, is_query, batch_size=8)
            except RuntimeError as exc2:
                if not self._is_oom(exc2):
                    raise
                # Last resort: move the model to CPU and finish there (slower, safe).
                self._free_memory()
                self._model = self._model.to("cpu")
                self.device = "cpu"
                return self._encode(texts, is_query, batch_size=16)
