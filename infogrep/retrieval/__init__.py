"""Retrieval backends and result fusion."""

from .base import Result, Retriever
from .fusion import reciprocal_rank_fusion

__all__ = ["Result", "Retriever", "reciprocal_rank_fusion"]
