"""Schema catalogue enabling multi-table reasoning.

Registrations are scoped to a session so one user's tables never appear in
another user's prompt context.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.core.database import db_mgr
from src.utils.logging import logger


ID_NAMES = frozenset({"id", "index", "pk", "uuid", "key"})


class SchemaRegistry:
    """Stores table shapes and infers plausible join keys between them."""

    @classmethod
    def register_dataframe(cls, filename: str, df: pd.DataFrame, session_id: str | None = None):
        try:
            columns = [str(column) for column in df.columns]
            meta = {
                "dtypes": {str(column): str(dtype) for column, dtype in df.dtypes.items()},
                "null_counts": {str(column): int(count) for column, count in df.isnull().sum().items()},
            }
            db_mgr.save_schema(
                filename=filename,
                columns=columns,
                row_count=int(len(df)),
                primary_key=cls._detect_primary_key(df),
                meta=meta,
                session_id=session_id,
            )
            logger.info("Registered dataset schema", filename=filename, columns=len(columns))
        except Exception as exc:
            logger.error("Failed to register schema", filename=filename, error=str(exc))

    @classmethod
    def _detect_primary_key(cls, df: pd.DataFrame) -> str:
        """Heuristic primary key: a conventional name first, then any unique column."""
        for column in df.columns:
            if str(column).lower() in ID_NAMES:
                return str(column)

        for column in df.columns:
            lowered = str(column).lower()
            if lowered.endswith("_id") or lowered.startswith("id_"):
                try:
                    if df[column].is_unique:
                        return str(column)
                except TypeError:
                    continue

        # A single vectorised null-count pass over the whole frame, rather than
        # one `Series.isnull().sum()` call per column from the Python loop --
        # on a wide DataFrame that per-column overhead was the bottleneck.
        # `is_unique`, the actually expensive check, then only runs on columns
        # that already passed the (cheap) null filter.
        try:
            null_counts = df.isnull().sum()
        except TypeError:
            return ""
        for column in df.columns:
            if null_counts[column] != 0:
                continue
            try:
                if df[column].is_unique:
                    return str(column)
            except TypeError:
                continue
        return ""

    # ------------------------------------------------------------------ #
    @classmethod
    def get_join_suggestions(cls, session_id: str | None = None) -> list[dict[str, Any]]:
        """Pairs of tables that share a column name or a name/key convention."""
        schemas = db_mgr.get_schemas(session_id=session_id)
        if len(schemas) < 2:
            return []

        suggestions = []
        for i in range(len(schemas)):
            for j in range(i + 1, len(schemas)):
                left, right = schemas[i], schemas[j]
                overlap = cls._find_overlap(left, right)
                if overlap:
                    suggestions.append(
                        {
                            "file1": left["filename"],
                            "file2": right["filename"],
                            "matching_columns": [{"col1": a, "col2": b} for a, b in overlap],
                        }
                    )
        return suggestions

    @staticmethod
    def _stem(filename: str) -> tuple[str, str]:
        name = filename.split(".")[0].lower()
        return name, name[:-1] if name.endswith("s") else name

    @classmethod
    def _find_overlap(cls, left: dict[str, Any], right: dict[str, Any]) -> list[tuple[str, str]]:
        left_name, left_singular = cls._stem(left["filename"])
        right_name, right_singular = cls._stem(right["filename"])

        overlap: list[tuple[str, str]] = []
        for column_a in left["columns"]:
            lowered_a = str(column_a).lower()
            for column_b in right["columns"]:
                lowered_b = str(column_b).lower()
                if lowered_a == lowered_b:
                    overlap.append((column_a, column_b))
                    continue
                # A foreign key convention: orders.user_id -> users.id
                if right.get("primary_key") and lowered_b == str(right["primary_key"]).lower():
                    if lowered_a in {f"{right_singular}_id", f"{right_name}_id"}:
                        overlap.append((column_a, column_b))
                elif left.get("primary_key") and lowered_a == str(left["primary_key"]).lower():
                    if lowered_b in {f"{left_singular}_id", f"{left_name}_id"}:
                        overlap.append((column_a, column_b))
        return overlap

    @classmethod
    def get_workspace_schema_context(cls, session_id: str | None = None) -> str:
        """Renders all registered tables for prompt injection."""
        schemas = db_mgr.get_schemas(session_id=session_id)
        if not schemas:
            return ""

        lines = ["\n=== WORKSPACE TABLES ==="]
        for schema in schemas:
            lines.append(f"Table: {schema['filename']}")
            lines.append(f"  {schema['row_count']} rows x {len(schema['columns'])} columns")
            if schema.get("primary_key"):
                lines.append(f"  Primary key: {schema['primary_key']}")
            dtypes = schema.get("meta", {}).get("dtypes", {})
            described = ", ".join(f"{c} ({dtypes.get(c, 'unknown')})" for c in schema["columns"][:30])
            lines.append(f"  Columns: {described}")

        suggestions = cls.get_join_suggestions(session_id=session_id)
        if suggestions:
            lines.append("--- Possible joins ---")
            for suggestion in suggestions:
                pairs = ", ".join(f"{m['col1']} <-> {m['col2']}" for m in suggestion["matching_columns"])
                lines.append(f"- {suggestion['file1']} and {suggestion['file2']} via {pairs}")

        return "\n".join(lines) + "\n"
