"""Pluggable embedding models. Default: Qwen3-Embedding-0.6B. (M3)

An embedder implements ``embed(texts: list[str]) -> np.ndarray``. New models are
registered by key (see ``DenseConfig.embedder``) so the dense store stays model-agnostic.
"""
