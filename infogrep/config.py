"""Configuration model and per-directory config loading.

A target directory's config lives at ``<dir>/.infogrep/config.toml``. Anything not
specified there falls back to the defaults below.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

# Name of the side-car directory written inside each indexed directory.
SIDECAR_DIRNAME = ".infogrep"


@dataclass
class ChunkConfig:
    """How long documents are split into passages."""

    size: int = 512  # target chunk size in tokens/words
    overlap: int = 64  # overlap between adjacent chunks


@dataclass
class IngestConfig:
    """Ingestion-side options."""

    ocr: bool = False  # OCR PDF pages that have little/no extractable text (needs tesseract)
    ocr_min_chars: int = 16  # below this many chars on a page, try OCR


@dataclass
class DenseConfig:
    """Dense retrieval settings.

    Off by default: embedding a large corpus needs a model download and significant
    RAM/GPU. Enable per directory with ``[dense] enabled = true`` once you want semantics.
    """

    enabled: bool = False
    embedder: str = "qwen"  # registry key; see infogrep.retrieval.embedders
    model_name: str = "Qwen/Qwen3-Embedding-0.6B"
    device: str = "auto"  # "auto" -> mps/cuda/cpu


@dataclass
class SparseConfig:
    """Sparse (Pyserini/BM25) settings."""

    enabled: bool = True
    prf: bool = False  # RM3 pseudo-relevance feedback, off by default


@dataclass
class KnowledgeBaseConfig:
    """Obsidian knowledge-base settings (backed by the Obsidian CLI)."""

    enabled: bool = False
    vault: str | None = None  # Obsidian vault name; None -> the CLI's active vault
    cli: str = "obsidian"  # path to the Obsidian CLI binary
    hops: int = 1  # graph link hops to expand (follows links + backlinks)
    search_limit: int = 10  # how many search hits to seed graph expansion from


@dataclass
class Config:
    """Top-level InfoGrep configuration for one indexed directory."""

    target_dir: Path
    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(
        default_factory=lambda: [".infogrep/**", ".git/**", "**/__pycache__/**"]
    )
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    sparse: SparseConfig = field(default_factory=SparseConfig)
    dense: DenseConfig = field(default_factory=DenseConfig)
    kb: KnowledgeBaseConfig = field(default_factory=KnowledgeBaseConfig)

    @property
    def sidecar_dir(self) -> Path:
        return self.target_dir / SIDECAR_DIRNAME

    @property
    def manifest_path(self) -> Path:
        return self.sidecar_dir / "manifest.sqlite"

    @property
    def sparse_dir(self) -> Path:
        return self.sidecar_dir / "sparse"

    @property
    def dense_dir(self) -> Path:
        return self.sidecar_dir / "dense"

    @property
    def cache_dir(self) -> Path:
        return self.sidecar_dir / "cache"

    @classmethod
    def load(cls, target_dir: str | Path) -> "Config":
        """Load config for ``target_dir``, applying defaults for anything unset."""
        target = Path(target_dir).expanduser().resolve()
        cfg = cls(target_dir=target)
        config_file = cfg.sidecar_dir / "config.toml"
        if config_file.is_file():
            with config_file.open("rb") as fh:
                data = tomllib.load(fh)
            cfg = cls._merge(cfg, data)
        return cfg

    @staticmethod
    def _merge(base: "Config", data: dict) -> "Config":
        """Shallow-merge a parsed TOML dict onto a default Config."""
        for key in ("include", "exclude"):
            if key in data:
                setattr(base, key, list(data[key]))
        if "chunk" in data:
            base.chunk = ChunkConfig(**{**asdict(base.chunk), **data["chunk"]})
        if "ingest" in data:
            base.ingest = IngestConfig(**{**asdict(base.ingest), **data["ingest"]})
        if "sparse" in data:
            base.sparse = SparseConfig(**{**asdict(base.sparse), **data["sparse"]})
        if "dense" in data:
            base.dense = DenseConfig(**{**asdict(base.dense), **data["dense"]})
        if "kb" in data:
            base.kb = KnowledgeBaseConfig(**{**asdict(base.kb), **data["kb"]})
        return base
