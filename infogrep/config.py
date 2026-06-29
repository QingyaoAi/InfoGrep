"""Configuration model and per-directory config loading.

Indexing never writes into the indexed folder. Each directory's index lives in a
separate location under ``$INFOGREP_HOME`` (default ``~/.infogrep``):
``$INFOGREP_HOME/indexes/<name>-<hash-of-abs-path>/``. Per-directory config is read from
that index dir's ``config.toml`` (with an optional global ``$INFOGREP_HOME/config.toml``
as a base).
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

# Legacy in-folder side-car name — still pruned during the walk so an old one (or a
# stray) inside a target never gets indexed. InfoGrep no longer creates it.
SIDECAR_DIRNAME = ".infogrep"


def index_home() -> Path:
    """Root for all InfoGrep indexes (override with the INFOGREP_HOME env var)."""
    return Path(os.environ.get("INFOGREP_HOME", "~/.infogrep")).expanduser()


def index_dir_for(target_dir: Path) -> Path:
    """Stable, separate index location for a target directory (outside the target)."""
    target = Path(target_dir).expanduser().resolve()
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", target.name) or "root"
    return index_home() / "indexes" / f"{name}-{digest}"


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
    workers: int = 0  # parallel extraction processes; 0 = auto (min(8, cpu count))


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
    # Analyzer language. Default "en+zh" handles English (Porter stemming) AND CJK
    # (bigrams) together. Also: "en" (English only), "zh"/"ja"/"ko" (single CJK).
    # Changing it triggers a full re-index.
    language: str = "en+zh"
    # Multi-field BM25 weights: passage text + file name + path.
    field_boosts: dict = field(
        default_factory=lambda: {"contents": 1.0, "filename": 2.0, "pathtext": 1.0}
    )


@dataclass
class KnowledgeBaseConfig:
    """Obsidian knowledge-base settings (backed by the Obsidian CLI)."""

    enabled: bool = False
    vault: str | None = None  # Obsidian vault name; None -> the CLI's active vault
    cli: str = "obsidian"  # path to the Obsidian CLI binary
    hops: int = 1  # graph link hops to expand (follows links + backlinks)
    search_limit: int = 10  # how many search hits to seed graph expansion from


# Documents indexed by content (and, where supported, OCR). Code/config files are not
# included by default — set include = ["**/*"] to index everything.
DEFAULT_DOC_TYPES = [
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "rtf",
    "odt", "ods", "odp", "txt", "md", "markdown", "rst", "tex", "csv", "tsv",
]
# Images: indexed by file name / path (content only if OCR is enabled).
DEFAULT_IMAGE_TYPES = [
    "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "svg", "heic", "heif",
]
DEFAULT_INCLUDE = [f"**/*.{ext}" for ext in DEFAULT_DOC_TYPES + DEFAULT_IMAGE_TYPES]

# Skip dependency / VCS / cache trees and editor/OS junk during the walk.
DEFAULT_EXCLUDE = [
    ".infogrep/**", "**/.git/**", "**/node_modules/**",
    "**/.venv/**", "**/venv/**", "**/site-packages/**", "**/__pycache__/**",
    "**/.cache/**", "**/.tox/**", "**/.mypy_cache/**", "**/.pytest_cache/**",
    "**/.Trash/**", "**/~$*", "**/.dropbox.cache/**",
]


@dataclass
class Config:
    """Top-level InfoGrep configuration for one indexed directory."""

    target_dir: Path
    # Documents + images by default; set include = ["**/*"] to index every file.
    include: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    sparse: SparseConfig = field(default_factory=SparseConfig)
    dense: DenseConfig = field(default_factory=DenseConfig)
    kb: KnowledgeBaseConfig = field(default_factory=KnowledgeBaseConfig)

    @property
    def index_dir(self) -> Path:
        """Where this directory's index lives — a separate location, not in the target."""
        return index_dir_for(self.target_dir)

    @property
    def manifest_path(self) -> Path:
        return self.index_dir / "manifest.sqlite"

    @property
    def sparse_dir(self) -> Path:
        return self.index_dir / "sparse"

    @property
    def dense_dir(self) -> Path:
        return self.index_dir / "dense"

    @property
    def cache_dir(self) -> Path:
        return self.index_dir / "cache"

    @classmethod
    def load(cls, target_dir: str | Path) -> "Config":
        """Load config for ``target_dir`` (global config.toml, then per-index override)."""
        target = Path(target_dir).expanduser().resolve()
        cfg = cls(target_dir=target)
        for config_file in (index_home() / "config.toml", cfg.index_dir / "config.toml"):
            if config_file.is_file():
                with config_file.open("rb") as fh:
                    cfg = cls._merge(cfg, tomllib.load(fh))
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
