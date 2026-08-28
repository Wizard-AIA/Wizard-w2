"""Session-scoped workspace file access.

The previous implementation mounted the entire shared workspace with
``StaticFiles`` at ``/workspace/static``, so any client could enumerate and
download every other user's dataset and generated files. Files are now served
per session through an explicit handler that resolves and re-checks the path.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from src.api.deps import get_session, get_session_for_link, require_api_key, require_dataset
from src.api.schemas import WorkspaceFile, WorkspaceListing
from src.core.session import Session


router = APIRouter(prefix="/api/workspace", tags=["workspace"])

PROTECTED_FILES = {"dataset.csv", "dataset.feather", "dataset.parquet"}

MEDIA_TYPES = {
    ".html": "text/html",
    ".csv": "text/csv",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".parquet": "application/octet-stream",
    ".feather": "application/octet-stream",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def classify(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
        return "image"
    if suffix == ".html":
        return "plot"
    if suffix in {".csv", ".tsv", ".xlsx", ".parquet", ".feather"}:
        return "table"
    if suffix in {".json", ".txt", ".md"}:
        return "text"
    return "file"


def resolve_within(root: Path, relative: str) -> Path:
    """Resolves ``relative`` under ``root``, refusing anything that escapes it.

    Both sides are fully resolved before comparison so symlinks and ``..``
    segments cannot be used to climb out of the session directory.
    """
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=403, detail="Path is outside the session workspace.")
    return candidate


class _ArrowChunkSink:
    """Small file-like sink that lets an IPC writer hand chunks to the client."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.position = 0
        self.closed = False

    def write(self, data: bytes) -> int:
        chunk = bytes(data)
        self.chunks.append(chunk)
        self.position += len(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        return None

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        # The IPC writer may close its sink after the end marker. Keep the
        # object readable by the generator until its pending bytes are drained.
        return None

    def drain(self) -> Iterator[bytes]:
        chunks, self.chunks = self.chunks, []
        yield from chunks


def _arrow_chunks(df, batch_size: int) -> Iterator[bytes]:
    """Encode a frame as one Arrow stream while retaining only pending chunks."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    sink = _ArrowChunkSink()
    # Infer schema from a small sample of real data so PyArrow can determine
    # correct types for object/string columns (an empty slice defaults to
    # pa.null() which crashes when actual rows are serialized against it).
    sample = df.head(min(1, len(df))) if len(df) > 0 else df
    schema = pa.Schema.from_pandas(sample, preserve_index=False)
    writer = ipc.new_stream(sink, schema)
    closed = False
    try:
        for start in range(0, len(df), batch_size):
            batch = pa.RecordBatch.from_pandas(df.iloc[start : start + batch_size], schema=schema, preserve_index=False)
            writer.write_batch(batch)
            yield from sink.drain()
        writer.close()
        closed = True
        yield from sink.drain()
    finally:
        if not closed:
            writer.close()


@router.get("/files", response_model=WorkspaceListing)
async def list_files(session: Session = Depends(get_session)) -> WorkspaceListing:
    root = session.workspace
    if not root.exists():
        return WorkspaceListing(files=[])

    files: list[WorkspaceFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        stat = path.stat()
        files.append(
            WorkspaceFile(
                name=path.name,
                path=str(path.relative_to(root)).replace(os.sep, "/"),
                size=stat.st_size,
                type=classify(path),
                modified_at=stat.st_mtime,
            )
        )
    return WorkspaceListing(files=files)


@router.get("/stream-arrow")
async def stream_arrow(
    dataset: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500_000),
    batch_size: int = Query(default=10_000, ge=1, le=100_000),
    sort_by: str | None = None,
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    session: Session = Depends(require_dataset),
) -> StreamingResponse:
    """Streams a session-scoped preview as an Apache Arrow IPC stream.

    Pagination keeps the browser request bounded while the binary format avoids
    the large intermediate JSON string and per-cell JSON object allocation used
    by the legacy preview route. ``limit`` can be raised by data tooling that
    genuinely needs a larger contiguous extract.
    """
    handle = session.datasets.get(dataset) if dataset else session.active_handle
    if handle is None:
        raise HTTPException(status_code=404, detail="Requested dataset is not loaded in this session.")

    frame = handle.df
    if sort_by:
        if sort_by not in frame.columns:
            raise HTTPException(status_code=400, detail=f"Unknown column '{sort_by}'.")
        frame = frame.sort_values(by=sort_by, ascending=sort_order == "asc", kind="stable")

    total_rows = len(frame)
    selected = frame.iloc[offset : offset + limit]
    headers = {
        "Cache-Control": "no-store",
        "X-Arrow-Total-Rows": str(total_rows),
        "X-Arrow-Offset": str(offset),
    }
    return StreamingResponse(
        _arrow_chunks(selected, batch_size),
        media_type="application/vnd.apache.arrow.stream",
        headers=headers,
    )


@router.get("/file/{file_path:path}")
async def get_file(file_path: str, session: Session = Depends(get_session_for_link)) -> FileResponse:
    """Serves one file from the caller's own workspace."""
    target = resolve_within(session.workspace, file_path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    is_plot = target.suffix.lower() == ".html"
    headers = {
        # Generated HTML charts are rendered in a sandboxed iframe; make sure
        # a stale chart is never served after a re-run.
        "Cache-Control": "no-store",
    }
    if is_plot:
        # Plotly writes raw HTML from dataset values (column names, labels). A
        # `sandbox` CSP is a second, server-side enforcement of the same
        # restrictions as the frontend's `<iframe sandbox="allow-scripts">`,
        # so a direct navigation to this URL (bypassing the iframe) is still
        # confined to a scriptable-but-isolated origin: no same-origin
        # access, no top-level navigation, no forms/popups.
        headers["Content-Security-Policy"] = "sandbox allow-scripts"
        headers["X-Content-Type-Options"] = "nosniff"

    return FileResponse(
        path=target,
        media_type=MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream"),
        filename=target.name,
        # Charts must render inline inside the iframe; every other workspace
        # file (datasets, exports) is a deliberate download via `download=`
        # anchors in the frontend, so `attachment` (the default) stays for
        # those.
        content_disposition_type="inline" if is_plot else "attachment",
        headers=headers,
    )


@router.delete("/file/{file_path:path}", dependencies=[Depends(require_api_key)])
async def delete_file(file_path: str, session: Session = Depends(get_session)) -> dict:
    target = resolve_within(session.workspace, file_path)
    if target.name in PROTECTED_FILES:
        raise HTTPException(status_code=400, detail="The active dataset cannot be deleted this way.")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    try:
        target.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete the file: {exc}")
    return {"message": f"Deleted '{target.name}'."}
