"""Optional cross-encoder reranking for high-precision retrieval."""

from __future__ import annotations

from dataclasses import dataclass

from src.utils.logging import logger


@dataclass
class RerankResult:
    """A single reranked passage with its cross-encoder score."""

    text: str
    score: float
    original_index: int


class CrossEncoderReranker:
    """Reranks first-stage retrieval candidates using a cross-encoder model.

    Falls back to identity (no reranking) if the required library is not
    installed or fails to load.
    """

    def __init__(self, model_name: str = "ms-marco-MiniLM-L-6-v2"):
        self._ranker = None
        self._available = False
        try:
            from flashrank import Ranker

            self._ranker = Ranker(model_name=model_name)
            self._available = True
            logger.info("cross_encoder_reranker_loaded", model=model_name)
        except (ImportError, Exception) as exc:
            logger.debug("cross_encoder_reranker_unavailable", reason=str(exc))

    @property
    def available(self) -> bool:
        return self._available

    def rerank(self, query: str, candidates: list[str], top_k: int = 5) -> list[RerankResult]:
        """Rerank candidates and return the top-k by cross-encoder score.

        If the cross-encoder is unavailable, returns the first *top_k*
        candidates unchanged with index-based scores.
        """
        if not self._available or not candidates:
            return [
                RerankResult(text=t, score=1.0 - i * 0.01, original_index=i) for i, t in enumerate(candidates[:top_k])
            ]
        try:
            from flashrank import RerankRequest

            passages = [{"text": c} for c in candidates]
            request = RerankRequest(query=query, passages=passages)
            results = self._ranker.rerank(request)
            return [
                RerankResult(
                    text=r["text"],
                    score=r["score"],
                    original_index=candidates.index(r["text"]),
                )
                for r in results[:top_k]
            ]
        except Exception as exc:
            logger.warning("cross_encoder_rerank_failed", error=str(exc))
            return [
                RerankResult(text=t, score=1.0 - i * 0.01, original_index=i) for i, t in enumerate(candidates[:top_k])
            ]


_reranker: CrossEncoderReranker | None = None


def get_reranker() -> CrossEncoderReranker:
    """Lazy singleton for the cross-encoder reranker."""
    global _reranker
    if _reranker is None:
        _reranker = CrossEncoderReranker()
    return _reranker
