"""Multi-format dataset ingestion with explicit large-file behaviour.

Replaces ``utils.validation.validate_csv``, which was CSV-only and materialised
the upload three times (raw bytes -> decoded ``str`` -> parsed frame), so a 50MB
file could cost well over 500MB of RAM.

What changed
------------
* Formats: csv/tsv/txt, xlsx/xls, json/ndjson, parquet, feather.
* Uploads stream to a temp file, so peak memory tracks the parsed frame rather
  than 3x the file.
* Frames larger than ``MAX_INMEMORY_ROWS`` are loaded in chunks and down-sampled
  deterministically, with the full copy still written to the workspace so the
  sandbox can stream it.
* Column names are sanitised **and de-duplicated**. The old regex stripped all
  punctuation, silently turning ``a-b`` and ``a.b`` into two columns both named
  ``ab``; that later broke Feather serialisation and made column selection
  ambiguous.
* Numeric columns are down-cast, which typically halves memory on wide frames.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import pandas as pd

from src.config import settings
from src.utils.logging import logger


class UnsupportedFormatError(ValueError):
    """Raised for a file extension the loader cannot parse."""


class EmptyDatasetError(ValueError):
    """Raised when a file parses successfully but contains no rows."""


CSV_LIKE = {".csv", ".tsv", ".txt", ".data"}
EXCEL_LIKE = {".xlsx", ".xls", ".xlsm"}
JSON_LIKE = {".json", ".ndjson", ".jsonl"}
PARQUET_LIKE = {".parquet", ".pq"}
FEATHER_LIKE = {".feather", ".ft"}

SUPPORTED_EXTENSIONS = CSV_LIKE | EXCEL_LIKE | JSON_LIKE | PARQUET_LIKE | FEATHER_LIKE

ENCODINGS = ("utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1")
DELIMITERS = (",", ";", "\t", "|")


@dataclass
class DatasetProfile:
    """Cheap structural facts computed once at load time."""

    rows: int
    columns: int
    memory_bytes: int
    truncated: bool = False
    original_rows: int | None = None
    renamed_columns: dict[str, str] = field(default_factory=dict)
    dropped_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "memory_bytes": self.memory_bytes,
            "truncated": self.truncated,
            "original_rows": self.original_rows,
            "renamed_columns": self.renamed_columns,
            "dropped_columns": self.dropped_columns,
        }


@dataclass
class LoadResult:
    df: pd.DataFrame
    profile: DatasetProfile
    source_format: str
    warnings: list[str] = field(default_factory=list)


def sanitize_columns(columns: list[Any]) -> tuple[list[str], dict[str, str]]:
    """Makes column names safe for code generation without creating collisions.

    Returns the new names plus a mapping of ``original -> final`` for every name
    that actually changed, so the change can be reported to the user rather than
    silently applied.
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    renamed: dict[str, str] = {}

    for position, raw in enumerate(columns):
        original = str(raw)
        # Collapse whitespace, drop characters that break attribute/query access.
        cleaned = re.sub(r"[^\w\s]", "_", original, flags=re.UNICODE)
        cleaned = re.sub(r"\s+", "_", cleaned.strip())
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")

        if not cleaned:
            cleaned = f"column_{position}"
        if cleaned[0].isdigit():
            cleaned = f"col_{cleaned}"

        # De-duplicate: the previous implementation stopped here and could emit
        # the same name twice.
        base = cleaned
        if base in seen:
            seen[base] += 1
            cleaned = f"{base}_{seen[base]}"
            while cleaned in seen:
                seen[base] += 1
                cleaned = f"{base}_{seen[base]}"
        seen[cleaned] = seen.get(cleaned, 0)

        if cleaned != original:
            renamed[original] = cleaned
        result.append(cleaned)

    return result, renamed


_CURRENCY_COLUMN_HINTS = ("price", "amount", "balance", "cost", "salary", "revenue", "total")


def downcast_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """Shrinks numeric dtypes in place where it is lossless.

    Integers always downcast losslessly (``pd.to_numeric`` refuses to narrow a
    column if it would drop a value). Floats do not have that guarantee — a
    float64 -> float32 cast can silently lose precision — so a column is only
    narrowed after confirming the round trip reproduces the original values,
    and currency-flavoured columns are left untouched even then.
    """
    for column in df.columns:
        dtype = df[column].dtype
        name = str(column).lower()
        try:
            if pd.api.types.is_integer_dtype(dtype):
                df[column] = pd.to_numeric(df[column], downcast="integer")
            elif pd.api.types.is_float_dtype(dtype) and not any(hint in name for hint in _CURRENCY_COLUMN_HINTS):
                narrowed = pd.to_numeric(df[column], downcast="float")
                original = df[column].to_numpy()
                if np.array_equal(np.asarray(narrowed, dtype="float64"), original, equal_nan=True):
                    df[column] = narrowed
        except (ValueError, TypeError):
            continue
    return df


def categorize_low_cardinality(df: pd.DataFrame, ratio: float | None = None) -> pd.DataFrame:
    """Converts repetitive object columns to ``category`` to cut memory."""
    if ratio is None:
        ratio = settings.CATEGORIZATION_RATIO_THRESHOLD
    row_count = len(df)
    if row_count < settings.CATEGORIZATION_MIN_ROWS:
        return df
    for column in df.select_dtypes(include=["object"]).columns:
        try:
            unique = df[column].nunique(dropna=False)
            if unique > 0 and unique / row_count < ratio:
                df[column] = df[column].astype("category")
        except (TypeError, ValueError):
            continue
    return df


class DatasetLoader:
    """Parses an uploaded file into a normalised DataFrame."""

    @staticmethod
    def is_supported(filename: str) -> bool:
        return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS

    @staticmethod
    def supported_extensions() -> list[str]:
        return sorted(SUPPORTED_EXTENSIONS)

    # ------------------------------------------------------------------ #
    @classmethod
    def spool_to_disk(cls, stream: BinaryIO, destination: Path, max_bytes: int) -> int:
        """Copies an upload to disk in fixed chunks, enforcing a size ceiling.

        Returns the byte count. Raises ``ValueError`` past ``max_bytes`` and
        removes the partial file so a rejected upload leaves nothing behind.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        chunk_size = 1024 * 1024
        try:
            with open(destination, "wb") as handle:
                while True:
                    chunk = stream.read(chunk_size)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"File too large. Maximum size is {max_bytes // (1024 * 1024)}MB.")
                    handle.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return written

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path, filename: str | None = None, max_rows: int | None = None) -> LoadResult:
        """Parses ``path`` into a DataFrame, normalising columns and dtypes."""
        name = filename or path.name
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise UnsupportedFormatError(
                f"Unsupported file type '{suffix or name}'. Supported: {', '.join(cls.supported_extensions())}"
            )

        limit = max_rows or settings.MAX_INMEMORY_ROWS
        warnings: list[str] = []

        if suffix in CSV_LIKE:
            df, truncated, original_rows, notes = cls._read_delimited(path, limit)
        elif suffix in EXCEL_LIKE:
            df, truncated, original_rows, notes = cls._read_excel(path, limit)
        elif suffix in JSON_LIKE:
            df, truncated, original_rows, notes = cls._read_json(path, suffix, limit)
        elif suffix in PARQUET_LIKE:
            df, truncated, original_rows, notes = cls._read_parquet(path, limit)
        else:
            df, truncated, original_rows, notes = cls._read_feather(path, limit)

        warnings.extend(notes)

        if df is None or df.empty:
            raise EmptyDatasetError("The uploaded file contains no rows.")

        # Flatten a MultiIndex header (common in exported spreadsheets).
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = ["_".join(str(part) for part in tup if str(part) != "nan") for tup in df.columns]
            warnings.append("Flattened a multi-level header into single column names.")

        columns, renamed = sanitize_columns(list(df.columns))
        df.columns = columns
        if renamed:
            warnings.append(f"Normalised {len(renamed)} column name(s) for safe code generation.")

        # Drop columns that are entirely empty; they only add prompt noise.
        empty_columns = [c for c in df.columns if df[c].isna().all()]
        if empty_columns:
            df = df.drop(columns=empty_columns)
            warnings.append(f"Dropped {len(empty_columns)} fully-empty column(s).")

        df = df.reset_index(drop=True)
        df = downcast_numeric(df)
        df = categorize_low_cardinality(df)

        profile = DatasetProfile(
            rows=len(df),
            columns=len(df.columns),
            memory_bytes=int(df.memory_usage(deep=True).sum()),
            truncated=truncated,
            original_rows=original_rows,
            renamed_columns=renamed,
            dropped_columns=empty_columns,
        )
        if truncated:
            warnings.append(
                f"Dataset exceeds the in-memory limit; analysis uses a {len(df):,}-row sample "
                f"of {original_rows:,} total rows. The full file remains available in the workspace."
            )

        return LoadResult(df=df, profile=profile, source_format=suffix.lstrip("."), warnings=warnings)

    # ------------------------------------------------------------------ #
    # Format readers. Each returns (df, truncated, original_rows, warnings).
    # ------------------------------------------------------------------ #
    @classmethod
    def _read_delimited(cls, path: Path, limit: int) -> tuple[pd.DataFrame, bool, int | None, list[str]]:
        encoding, delimiter = cls._sniff(path)
        notes: list[str] = []

        read_kwargs: dict[str, Any] = {
            "encoding": encoding,
            "sep": delimiter,
            "on_bad_lines": "skip",
            "skip_blank_lines": True,
        }

        # Count rows cheaply first so we know whether sampling is needed.
        total_rows = cls._count_rows(path, encoding)
        if total_rows is not None and total_rows > limit:
            frames: list[pd.DataFrame] = []
            # Deterministic systematic sample: keep every Nth row.
            stride = max(1, total_rows // limit)
            kept = 0
            for index, chunk in enumerate(pd.read_csv(path, chunksize=100_000, **read_kwargs)):
                subset = chunk.iloc[::stride] if stride > 1 else chunk
                frames.append(subset)
                kept += len(subset)
                if kept >= limit:
                    break
                del chunk, index
            df = pd.concat(frames, ignore_index=True).head(limit)
            return df, True, total_rows, notes

        try:
            df = pd.read_csv(path, **read_kwargs)
        except Exception as exc:
            notes.append(f"Strict parse failed ({exc}); retried with delimiter auto-detection.")
            df = pd.read_csv(path, encoding=encoding, sep=None, engine="python", on_bad_lines="skip")
        return df, False, total_rows, notes

    @classmethod
    def _read_excel(cls, path: Path, limit: int) -> tuple[pd.DataFrame, bool, int | None, list[str]]:
        notes: list[str] = []
        try:
            sheets = pd.read_excel(path, sheet_name=None)
        except ImportError as exc:
            raise UnsupportedFormatError(
                "Reading Excel files requires the 'openpyxl' package to be installed."
            ) from exc

        if not sheets:
            raise EmptyDatasetError("The workbook contains no sheets.")

        # Pick the largest sheet; report the choice rather than silently guessing.
        name, df = max(sheets.items(), key=lambda item: len(item[1]))
        if len(sheets) > 1:
            notes.append(f"Workbook has {len(sheets)} sheets; loaded the largest ('{name}').")

        original = len(df)
        if original > limit:
            return df.head(limit), True, original, notes
        return df, False, original, notes

    @classmethod
    def _read_json(cls, path: Path, suffix: str, limit: int) -> tuple[pd.DataFrame, bool, int | None, list[str]]:
        notes: list[str] = []
        lines = suffix in {".ndjson", ".jsonl"}
        try:
            df = pd.read_json(path, lines=lines)
        except ValueError:
            # Nested payload: normalise the first list-valued key we can find.
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                records = next((v for v in payload.values() if isinstance(v, list)), None)
                if records is None:
                    records = [payload]
                    notes.append("JSON object had no array field; treated it as a single record.")
                else:
                    notes.append("Extracted the first array field from the JSON object.")
            else:
                records = payload
            df = pd.json_normalize(records)

        original = len(df)
        if original > limit:
            return df.head(limit), True, original, notes
        return df, False, original, notes

    @classmethod
    def _read_parquet(cls, path: Path, limit: int) -> tuple[pd.DataFrame, bool, int | None, list[str]]:
        df = pd.read_parquet(path)
        original = len(df)
        if original > limit:
            return df.head(limit), True, original, []
        return df, False, original, []

    @classmethod
    def _read_feather(cls, path: Path, limit: int) -> tuple[pd.DataFrame, bool, int | None, list[str]]:
        df = pd.read_feather(path)
        original = len(df)
        if original > limit:
            return df.head(limit), True, original, []
        return df, False, original, []

    # ------------------------------------------------------------------ #
    @staticmethod
    def _sniff(path: Path) -> tuple[str, str]:
        """Detects encoding and delimiter from the first block of the file."""
        head = path.open("rb").read(64 * 1024)

        encoding = "utf-8"
        # UTF-16 encodes ASCII with interleaved NULs, which UTF-8 never produces.
        if b"\x00" in head:
            encoding = "utf-16"
        else:
            for candidate in ENCODINGS:
                try:
                    head.decode(candidate)
                    encoding = candidate
                    break
                except UnicodeDecodeError:
                    continue

        try:
            sample = head.decode(encoding, errors="ignore")
        except LookupError:
            sample = head.decode("utf-8", errors="ignore")

        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        delimiter = max(DELIMITERS, key=first_line.count)
        if first_line.count(delimiter) == 0:
            delimiter = ","
        return encoding, delimiter

    @staticmethod
    def _count_rows(path: Path, encoding: str) -> int | None:
        """Counts data rows without parsing. Returns None if it cannot be done cheaply."""
        try:
            with open(path, encoding=encoding, errors="ignore") as handle:
                count = sum(1 for _ in handle)
            return max(0, count - 1)  # discount the header
        except Exception:
            return None


def safe_write_feather(df: pd.DataFrame, path: Path) -> bool:
    """Writes Feather, coercing unsupported object columns to text on failure.

    Returns True when the original dtypes survived, False when coercion happened,
    so callers can tell the user their column types changed.
    """
    try:
        df.to_feather(path)
        return True
    except Exception:
        coerced = df.copy()
        for column in coerced.columns:
            if coerced[column].dtype == "object":
                try:
                    coerced[column] = coerced[column].astype(str)
                except Exception:
                    coerced[column] = coerced[column].apply(repr)
        try:
            coerced.to_feather(path)
        except Exception as exc:
            logger.warning("Feather write failed after coercion", error=str(exc))
            raise
        return False


def json_safe_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Converts a frame to JSON-serialisable records.

    ``NaN``/``Inf`` are not valid JSON and previously escaped through
    ``df.replace(...)`` for some dtypes, producing a 500 from the preview route.
    Going through numpy object coercion catches every case.
    """
    if df.empty:
        return []
    prepared = df.copy()
    for column in prepared.columns:
        series = prepared[column]
        if pd.api.types.is_datetime64_any_dtype(series):
            prepared[column] = series.astype(str)
        elif isinstance(series.dtype, pd.CategoricalDtype):
            prepared[column] = series.astype(str)

    prepared = prepared.replace([np.inf, -np.inf], np.nan)
    prepared = prepared.astype(object).where(pd.notnull(prepared), None)
    return prepared.to_dict(orient="records")


def make_temp_path(suffix: str = "", workspace: Path | None = None) -> Path:
    """Temp file for an in-flight upload, named and created atomically.

    ``mkstemp`` already rules out the symlink pre-creation race a predictable
    name would allow -- it opens with ``O_CREAT | O_EXCL``. What a shared
    ``DATA_DIR/uploads`` still gets wrong is scope: every session's in-flight
    upload sits in one directory, readable by anything with access to it on a
    shared machine, for however long parsing takes. A caller with a session
    passes its private workspace instead, so an upload never has a moment
    where it exists outside the one place already scoped to that session.
    ``DATA_DIR/uploads`` remains the fallback for callers with no session in
    hand.
    """
    tmp_dir = workspace / "uploads" if workspace is not None else settings.DATA_DIR / "uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(suffix=suffix, dir=str(tmp_dir))
    os.close(handle)
    return Path(name)


def cleanup_path(path: Path | None):
    if path is None:
        return
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove a temporary ingest path", path=str(path), error=str(exc))
