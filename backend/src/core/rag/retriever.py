"""Retrieval over the agent's own history and the workspace schemas.

Motivation
----------
``generate_system_context`` unconditionally dumped ``df.info()``, ``df.describe()``,
every categorical column's unique values *and* every registered workspace schema
into every prompt. On a wide frame that alone can exceed the context window,
which is the real cause of the "model ignores my columns" failure mode.

This module replaces the dump with retrieval:

* **Column retrieval** - rank columns by relevance to the question so a
  200-column frame contributes only the columns that matter, plus any the user
  named verbatim.
* **Memory retrieval** - semantic search over prior interactions instead of the
  previous ``LIKE %term%`` scan, which matched on stopwords.
* **Schema retrieval** - only surface other workspace tables that plausibly join
  to the active one.

Everything degrades gracefully: with no embedding model the service falls back to
lexical overlap scoring, and with no history it returns empty context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.config import settings
from src.core.database import db_mgr
from src.core.embeddings import embedding_service
from src.utils.logging import logger


STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "by",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "show",
        "me",
        "please",
        "can",
        "you",
        "what",
        "which",
        "how",
        "many",
        "much",
        "give",
        "get",
        "make",
        "plot",
        "chart",
        "graph",
        "data",
        "dataset",
        "column",
        "columns",
        "row",
        "rows",
        "value",
        "values",
        "using",
        "use",
        "from",
        "that",
        "this",
        "it",
        "all",
    }
)


@dataclass
class RetrievedChunk:
    text: str
    score: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "score": round(self.score, 4), "source": self.source}


def tokenize(text: str) -> set[str]:
    """Content words only, lowercased."""
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{1,}", str(text).lower())
    return {t for t in tokens if t not in STOPWORDS}


def mentions_column(query: str, column: str) -> bool:
    """Whether ``query`` names ``column``, matched on word boundaries.

    A substring test looks equivalent and is not: a column called ``C`` matches
    inside "check", ``id`` matches inside "provide", ``n`` matches everywhere.
    Short column names are extremely common, so a substring test reports almost
    every column as explicitly requested and the relevance budget stops
    budgeting anything.
    """
    name = str(column).strip()
    if not name:
        return False
    return re.search(rf"(?<![a-zA-Z0-9_]){re.escape(name.lower())}(?![a-zA-Z0-9_])", str(query).lower()) is not None


def lexical_overlap(query_tokens: set[str], candidate: str) -> float:
    """Jaccard-ish overlap used when no embedding model is available."""
    candidate_tokens = tokenize(candidate)
    if not query_tokens or not candidate_tokens:
        return 0.0
    intersection = len(query_tokens & candidate_tokens)
    if intersection == 0:
        return 0.0
    return intersection / len(query_tokens | candidate_tokens)


class ContextRetriever:
    """Selects the slices of available context that are worth spending tokens on."""

    def __init__(self, top_k: int | None = None, min_similarity: float | None = None):
        self.top_k = top_k or settings.RAG_TOP_K
        self.min_similarity = min_similarity if min_similarity is not None else settings.RAG_MIN_SIMILARITY

    # ------------------------------------------------------------------ #
    # Column selection
    # ------------------------------------------------------------------ #
    def select_columns(
        self,
        query: str,
        df: pd.DataFrame,
        max_columns: int | None = None,
        plan: str | None = None,
    ) -> tuple[list[str], bool]:
        """Returns (columns_to_describe, was_truncated).

        Columns the user names explicitly are always kept. Remaining slots go to
        the highest-scoring columns, with numeric columns favoured slightly since
        they carry most analytical intent.
        """
        limit = max_columns or settings.PROMPT_MAX_COLUMNS
        all_columns = list(df.columns)
        if len(all_columns) <= limit:
            return all_columns, False

        query_tokens = tokenize(query)

        explicit = {c for c in all_columns if mentions_column(query, c)}
        if plan:
            plan_mentions = {c for c in all_columns if mentions_column(plan, c)}
            explicit = explicit | plan_mentions

        explicit_list = list(explicit)
        remaining = [c for c in all_columns if c not in explicit_list]

        scored: list[tuple[float, str]] = []
        for column in remaining:
            score = lexical_overlap(query_tokens, str(column))
            scored.append((score, str(column)))
        scored.sort(key=lambda item: item[0], reverse=True)

        slots = max(0, limit - len(explicit_list))
        selected = explicit_list + [name for _, name in scored[:slots]]
        # Preserve the frame's original column order for readability.
        ordered = [c for c in all_columns if c in set(selected)]
        return ordered, True

    # ------------------------------------------------------------------ #
    # Memory retrieval
    # ------------------------------------------------------------------ #
    def retrieve_memories(self, query: str, session_id: str | None = None) -> list[RetrievedChunk]:
        """Semantically relevant prior interactions for this session."""
        if not settings.RAG_ENABLED:
            return []

        try:
            memories = db_mgr.get_memories(session_id=session_id, limit=200)
        except Exception as exc:
            logger.warning("Memory retrieval failed", error=str(exc))
            return []

        if not memories:
            return []

        candidates = [(m.get("instruction") or "", m.get("embedding")) for m in memories]
        ranked = embedding_service.rank(query, candidates)

        chunks: list[RetrievedChunk] = []
        for score, index in ranked[: self.top_k]:
            if score < self.min_similarity:
                continue
            memory = memories[index]
            result = (memory.get("result") or "").strip()
            summary = result[:280] + ("..." if len(result) > 280 else "")
            chunks.append(
                RetrievedChunk(
                    text=f"Earlier request: {memory.get('instruction')}\nOutcome: {summary}",
                    score=score,
                    source="memory",
                )
            )
        return chunks

    def retrieve_trajectories(self, query: str, columns: list[str] | None) -> RetrievedChunk | None:
        """The closest past failure-then-fix for this schema, if similar enough."""
        try:
            entries = db_mgr.get_trajectory_entries(columns)
        except Exception as exc:
            logger.warning("Trajectory retrieval failed", error=str(exc))
            return None
        if not entries:
            return None

        candidates = [(e.get("instruction") or "", e.get("embedding")) for e in entries]
        ranked = embedding_service.rank(query, candidates)
        if not ranked:
            return None

        score, index = ranked[0]
        if score < settings.TRAJECTORY_MIN_SIMILARITY:
            return None

        entry = entries[index]
        text = (
            "A similar request previously failed. Do not repeat this mistake.\n"
            f"Failed code:\n```python\n{entry['failed_code']}\n```\n"
            f"Error:\n{entry['error_message']}\n"
            f"Working solution:\n```python\n{entry['corrected_code']}\n```"
        )
        logger.info("Matched negative trajectory", similarity=round(score, 4))
        return RetrievedChunk(text=text, score=score, source="trajectory")

    def retrieve_examples(self, query: str, limit: int = 2) -> list[dict[str, str]]:
        """Few-shot successes ranked semantically."""
        try:
            feedbacks = db_mgr.get_feedbacks()
        except Exception as exc:
            logger.warning("Feedback retrieval failed", error=str(exc))
            return []
        if not feedbacks:
            return []

        candidates = [(f.get("task") or "", f.get("embedding")) for f in feedbacks]
        ranked = embedding_service.rank(query, candidates)

        results: list[dict[str, str]] = []
        for score, index in ranked[:limit]:
            if score < self.min_similarity:
                continue
            entry = feedbacks[index]
            results.append({"task": entry["task"], "code": entry["code"]})
        return results

    # ------------------------------------------------------------------ #
    # Schema retrieval
    # ------------------------------------------------------------------ #
    def retrieve_related_schemas(
        self,
        query: str,
        session_id: str | None,
        active_columns: list[str],
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Other workspace tables that share a join key or match the question."""
        try:
            schemas = db_mgr.get_schemas(session_id=session_id)
        except Exception as exc:
            logger.warning("Schema retrieval failed", error=str(exc))
            return []
        if len(schemas) <= 1:
            return []

        active = {str(c).lower() for c in active_columns}
        query_tokens = tokenize(query)

        scored: list[tuple[float, dict[str, Any]]] = []
        for schema in schemas:
            columns = {str(c).lower() for c in schema.get("columns", [])}
            if columns == active:
                continue  # this is the active table
            shared = len(columns & active)
            name_score = lexical_overlap(query_tokens, schema.get("filename", ""))
            column_score = max(
                (lexical_overlap(query_tokens, c) for c in schema.get("columns", [])),
                default=0.0,
            )
            # A shared column is a concrete join opportunity and outranks fuzzy text matching.
            score = shared * 1.0 + name_score + column_score
            if score > 0:
                scored.append((score, schema))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [schema for _, schema in scored[:limit]]

    # ------------------------------------------------------------------ #
    def build_context_block(self, query: str, session_id: str | None) -> str:
        """Renders retrieved memories into a prompt block, or "" when nothing is relevant."""
        chunks = self.retrieve_memories(query, session_id=session_id)
        if not chunks:
            return ""
        body = "\n\n".join(chunk.text for chunk in chunks)
        return f"\n<relevant_history>\n{body}\n</relevant_history>\n"


context_retriever = ContextRetriever()
