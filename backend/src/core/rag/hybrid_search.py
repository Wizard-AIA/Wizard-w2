"""Reciprocal Rank Fusion for sparse-dense hybrid search."""

from __future__ import annotations

from collections import defaultdict


def reciprocal_rank_fusion(
    *rank_lists: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """Combine multiple ranked result lists using Reciprocal Rank Fusion.

    Each rank list is a sequence of ``(doc_id, score)`` tuples ordered by
    descending relevance.  RRF assigns each document a fused score:

        RRF(d) = sum(1 / (k + rank_i(d))) for each list i

    Args:
        *rank_lists: One or more ranked result lists.
        k: Smoothing constant (default 60, per the original RRF paper).

    Returns:
        Fused results sorted by descending RRF score.
    """
    scores: dict[str, float] = defaultdict(float)
    for results in rank_lists:
        for rank, (doc_id, _score) in enumerate(results):
            scores[doc_id] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
