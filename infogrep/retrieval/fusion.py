"""Reciprocal Rank Fusion (RRF) for combining multiple result lists.

RRF needs no score normalization or tuning: each result contributes
``1 / (k + rank)`` from every list it appears in. See Cormack et al., 2009.
"""

from __future__ import annotations

from dataclasses import replace

from .base import Result


def reciprocal_rank_fusion(
    result_lists: list[list[Result]], k: int = 60, top_n: int = 10
) -> list[Result]:
    """Fuse several ranked result lists into one, deduplicating by passage.

    Args:
        result_lists: one ranked list per retriever.
        k: RRF damping constant (60 is the standard default).
        top_n: how many fused results to return.
    """
    scores: dict[str, float] = {}
    best: dict[str, Result] = {}
    for results in result_lists:
        for rank, res in enumerate(results):
            key = f"{res.doc_id}:{res.passage_id}"
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            # Keep the highest-scoring single-retriever instance for provenance.
            if key not in best or res.score > best[key].score:
                best[key] = res

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    # replace() keeps all fields (incl. file metadata), overriding only score/retriever.
    return [
        replace(best[key], score=fused_score, retriever="hybrid")
        for key, fused_score in ordered[:top_n]
    ]
