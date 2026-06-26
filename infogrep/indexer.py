"""Index orchestration: ingest -> chunk -> embed -> write sparse + dense indices.

Implemented across M1-M3. Coordinates the manifest, extractors, chunker, and the
sparse/dense backends, doing incremental (delta-only) work on re-index.
"""

from __future__ import annotations

from .config import Config


class Indexer:
    """Builds and incrementally updates a directory's side-car index. (M1-M3)"""

    def __init__(self, config: Config):
        self.config = config

    def reindex(self, full: bool = False) -> None:
        raise NotImplementedError("Indexer lands in M1 (ingestion) and grows through M3.")

    def status(self) -> dict:
        raise NotImplementedError("Index status lands in M1.")
