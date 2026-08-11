"""Session-scoped workspace file access.

The previous implementation mounted the entire shared workspace with
``StaticFiles`` at ``/workspace/static``, so any client could enumerate and
download every other user's dataset and generated files. Files are now served
per session through an explicit handler that resolves and re-checks the path.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from src.api.deps import get_session, get_session_for_link, require_api_key
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
