"""Dense retriever backed by Zvec + a pluggable embedder.

Zvec stores only ``passage_id -> vector`` (in-process, disk-backed). Result metadata
(path/page/snippet) is enriched from the manifest, so there is one source of truth.
Embeddings are cached by text hash, so unchanged passages are never re-embedded.
"""

from __future__ import annotations

import json
import shutil
from typing import Iterable

from typing import TYPE_CHECKING

from ..config import Config
from .base import Result

if TYPE_CHECKING:
    from .embedders.cache import EmbeddingCache

_VECTOR_FIELD = "embedding"
_BUILD_BATCH = 128
_SNIPPET_CHARS = 240

DENSE_EXTRA_HINT = (
    "dense search needs the optional 'dense' dependencies. Install with "
    "`pip install 'infogrep[dense]'` (from a checkout: `uv sync --extra dense`)."
)


def _import_zvec():
    try:
        import zvec
    except ImportError as exc:
        raise RuntimeError(DENSE_EXTRA_HINT) from exc
    return zvec


class DenseIndex:
    """Build and query a Zvec vector index over passages."""

    name = "dense"

    def __init__(self, config: Config):
        self.config = config
        self.dense_dir = config.dense_dir
        self.cache_dir = config.cache_dir
        self.manifest_path = config.manifest_path
        self._embedder = None

    @property
    def embedder(self):
        if self._embedder is None:
            # Deferred: the embedders package needs numpy, which (like the rest of the
            # embedding stack) is only installed with the optional 'dense' extra.
            from .embedders.registry import get_embedder

            self._embedder = get_embedder(self.config.dense)
        return self._embedder

    @property
    def _meta_path(self):
        return self.dense_dir / "embedder.json"

    def _exists(self) -> bool:
        # The meta file is written only after a successful build, so it marks a
        # *complete* index — a partial/aborted build (e.g. OOM) won't have it.
        return self.dense_dir.is_dir() and self._meta_path.is_file()

    def _write_meta(self, name: str, dim: int) -> None:
        self._meta_path.write_text(json.dumps({"name": name, "dim": dim}))

    def _check_embedder_matches(self) -> None:
        """Fail clearly if config selects a different embedder than the index was built with."""
        if not self._meta_path.is_file():
            return
        built = json.loads(self._meta_path.read_text())
        if built.get("name") != self.embedder.name:
            raise FileNotFoundError(
                f"dense index was built with embedder '{built.get('name')}' but config "
                f"selects '{self.embedder.name}'. Run `infogrep index <dir> --full` to rebuild."
            )

    # -- build -------------------------------------------------------------

    def build(self, passages: Iterable) -> int:
        """(Re)build the Zvec collection from a stream of manifest passage Rows."""
        zvec = _import_zvec()
        from .embedders.cache import EmbeddingCache

        embedder = self.embedder
        dim = embedder.dim
        cache = EmbeddingCache(self.cache_dir / "embeddings.sqlite", embedder.name)

        if self.dense_dir.exists():
            shutil.rmtree(self.dense_dir)
        self.dense_dir.parent.mkdir(parents=True, exist_ok=True)

        schema = zvec.CollectionSchema(
            name="passages",
            vectors=zvec.VectorSchema(
                _VECTOR_FIELD,
                zvec.DataType.VECTOR_FP32,
                dim,
                zvec.FlatIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
        )
        collection = zvec.create_and_open(path=str(self.dense_dir), schema=schema)

        n = 0
        try:
            for batch in _batched(passages, _BUILD_BATCH):
                vectors = self._embed_with_cache(
                    [r["text"] for r in batch], cache, is_query=False
                )
                docs = [
                    zvec.Doc(id=row["passage_id"], vectors={_VECTOR_FIELD: vec.tolist()})
                    for row, vec in zip(batch, vectors)
                ]
                collection.insert(docs)
                n += len(docs)
                # Release GPU cache between batches so RAM stays bounded on big corpora.
                free = getattr(embedder, "_free_memory", None)
                if free:
                    free()
            collection.flush()
            self._write_meta(embedder.name, dim)
        finally:
            cache.close()
        return n

    def built_embedder_name(self) -> str | None:
        """The embedder a complete index was built with (without loading any model)."""
        if self._meta_path.is_file():
            return json.loads(self._meta_path.read_text()).get("name")
        return None

    def update(self, removed_ids, added_passages) -> int:
        """Incrementally apply a delta to an existing Zvec collection.

        Deletes ``removed_ids`` and (up)inserts ``added_passages`` — no full rebuild.
        The embedding model is loaded lazily, so a delete-only update never loads it.
        """
        zvec = _import_zvec()
        from .embedders.cache import EmbeddingCache

        collection = zvec.open(path=str(self.dense_dir))
        removed = list(removed_ids)
        if removed:
            try:
                collection.delete(removed)
            except Exception:  # tolerate ids already gone
                for rid in removed:
                    try:
                        collection.delete(rid)
                    except Exception:
                        pass

        cache: EmbeddingCache | None = None
        n = 0
        for batch in _batched(added_passages, _BUILD_BATCH):
            if cache is None:  # first batch: now we actually need the model
                cache = EmbeddingCache(self.cache_dir / "embeddings.sqlite", self.embedder.name)
            vectors = self._embed_with_cache([r["text"] for r in batch], cache, is_query=False)
            docs = [
                zvec.Doc(id=row["passage_id"], vectors={_VECTOR_FIELD: vec.tolist()})
                for row, vec in zip(batch, vectors)
            ]
            collection.upsert(docs)
            n += len(docs)
            free = getattr(self.embedder, "_free_memory", None)
            if free:
                free()

        collection.flush()
        if cache is not None:
            self._write_meta(self.embedder.name, self.embedder.dim)
            cache.close()
        return n

    def _embed_with_cache(self, texts: list[str], cache: EmbeddingCache, is_query: bool):
        keys = [cache.key(t) for t in texts]
        cached = cache.get_many(keys)
        missing_idx = [i for i, k in enumerate(keys) if k not in cached]
        if missing_idx:
            fresh = self.embedder.embed([texts[i] for i in missing_idx], is_query=is_query)
            cache.put_many([(keys[i], fresh[j]) for j, i in enumerate(missing_idx)])
            for j, i in enumerate(missing_idx):
                cached[keys[i]] = fresh[j]
        return [cached[k] for k in keys]

    # -- search ------------------------------------------------------------

    def search(self, query: str, k: int = 10) -> list[Result]:
        zvec = _import_zvec()

        if not self._exists():
            raise FileNotFoundError(
                f"No dense index at {self.dense_dir}. Run `infogrep index <dir>` first."
            )
        self._check_embedder_matches()

        qvec = self.embedder.embed([query], is_query=True)[0]
        collection = zvec.open(path=str(self.dense_dir))
        hits = collection.query(zvec.Query(_VECTOR_FIELD, vector=qvec.tolist()), topk=k)

        ids = [_hit_id(h) for h in hits]
        meta = self._lookup_metadata(ids)
        results: list[Result] = []
        for hit in hits:
            pid = _hit_id(hit)
            row = meta.get(pid)
            if row is None:
                continue
            text = row["text"]
            results.append(
                Result(
                    doc_id=row["path"],
                    passage_id=pid,
                    path=row["path"],
                    snippet=text[:_SNIPPET_CHARS],
                    # Zvec COSINE returns distance (1 - cos); report similarity so
                    # higher = better, consistent with the sparse retriever.
                    score=1.0 - float(_hit_score(hit)),
                    retriever="dense",
                    page=row["page"],
                    offset=row["offset"],
                )
            )
        return results

    def _lookup_metadata(self, ids: list[str]) -> dict:
        from ..manifest import Manifest

        with Manifest(self.manifest_path) as manifest:
            return manifest.get_passages_by_ids(ids)


def _batched(iterable: Iterable, size: int):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _hit_id(hit):
    return hit["id"] if isinstance(hit, dict) else hit.id


def _hit_score(hit):
    return hit["score"] if isinstance(hit, dict) else hit.score
