"""Dataset upload, listing and preview."""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile

from src.api.deps import SESSION_HEADER, get_session, require_api_key, require_dataset
from src.api.schemas import (
    DatasetSummary,
    DocumentSummary,
    DocumentUploadResponse,
    PreviewResponse,
    SessionResponse,
    UploadResponse,
)
from src.config import settings
from src.core.agent.flow import science_agent
from src.core.ingest.documents import (
    DocumentExtractionError,
    UnsupportedDocumentError,
    is_supported_document,
    load_document,
    supported_document_extensions,
)
from src.core.ingest.loader import (
    DatasetLoader,
    EmptyDatasetError,
    UnsupportedFormatError,
    cleanup_path,
    json_safe_records,
    make_temp_path,
)
from src.core.session import Session
from src.core.tools.schema_registry import SchemaRegistry
from src.utils.logging import logger


router = APIRouter(prefix="/api", tags=["datasets"])


@router.post("/datasets", response_model=UploadResponse, dependencies=[Depends(require_api_key)])
async def upload_dataset(
    response: Response,
    file: UploadFile = File(...),
    clean: bool = Query(default=True, description="Run the automatic semantic cleaning pass."),
    session: Session = Depends(get_session),
) -> UploadResponse:
    """Ingests a file into the caller's session.

    The upload is streamed to disk first so peak memory tracks the parsed frame
    rather than three copies of the raw bytes.
    """
    filename = os.path.basename(file.filename or "dataset.csv")
    if not DatasetLoader.is_supported(filename):
        raise HTTPException(
            status_code=422,
            detail=(f"Unsupported file type. Supported formats: {', '.join(DatasetLoader.supported_extensions())}"),
        )

    temp_path = make_temp_path(suffix=Path(filename).suffix, workspace=session.workspace)
    try:
        try:
            await asyncio.to_thread(DatasetLoader.spool_to_disk, file.file, temp_path, settings.MAX_UPLOAD_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))

        try:
            load_result = await asyncio.to_thread(DatasetLoader.load, temp_path, filename)
        except UnsupportedFormatError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except EmptyDatasetError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.error("Dataset parse failed", filename=filename, error=str(exc))
            raise HTTPException(status_code=400, detail=f"Could not parse the file: {exc}")

        df = load_result.df
        warnings = list(load_result.warnings)

        cleaning_summary = "Automatic cleaning was skipped."
        catalog: dict = {}
        if clean:
            try:
                df, catalog, cleaning_summary = await asyncio.to_thread(
                    science_agent.clean_dataset, df, session, filename
                )
            except Exception as exc:
                logger.warning("Cleaning stage failed", error=str(exc))
                warnings.append("Automatic cleaning failed; the raw data was kept.")
        if not catalog:
            from src.core.tools.catalog import CatalogEngine

            catalog = await asyncio.to_thread(CatalogEngine.analyze, df)

        handle = session.add_dataset(
            name=filename,
            df=df,
            catalog=catalog,
            profile=load_result.profile.to_dict(),
            source_format=load_result.source_format,
            make_active=True,
        )

        SchemaRegistry.register_dataframe(filename, df, session_id=session.id)
        session.executor.reload_dataset()

        logger.info("Dataset ingested", filename=filename, rows=len(df), session=session.id)
        response.headers[SESSION_HEADER] = session.id

        return UploadResponse(
            message="Dataset loaded.",
            dataset=DatasetSummary(**handle.summary()),
            cleaning_result=cleaning_summary,
            warnings=warnings,
            catalog=catalog,
            session_id=session.id,
        )
    finally:
        cleanup_path(temp_path)


@router.get("/datasets", response_model=SessionResponse)
async def list_datasets(response: Response, session: Session = Depends(get_session)) -> SessionResponse:
    response.headers[SESSION_HEADER] = session.id
    return SessionResponse(**session.describe())


@router.post("/documents", response_model=DocumentUploadResponse, dependencies=[Depends(require_api_key)])
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> DocumentUploadResponse:
    """Attaches a reference document — a data dictionary, rules, definitions.

    These are not data. They are the context that says what the data *means*,
    and the agent retrieves from them mid-analysis rather than having them
    pasted into every prompt.
    """
    if not settings.CONTEXT_DOCS_ENABLED:
        raise HTTPException(status_code=403, detail="Context documents are disabled on this deployment.")

    filename = os.path.basename(file.filename or "document.md")
    if not is_supported_document(filename):
        raise HTTPException(
            status_code=422,
            detail=(f"Unsupported document type. Supported formats: {', '.join(supported_document_extensions())}"),
        )

    temp_path = make_temp_path(suffix=Path(filename).suffix, workspace=session.workspace)
    try:
        try:
            await asyncio.to_thread(DatasetLoader.spool_to_disk, file.file, temp_path, settings.CONTEXT_DOC_MAX_BYTES)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc))

        try:
            document = await asyncio.to_thread(load_document, temp_path, filename)
        except UnsupportedDocumentError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except DocumentExtractionError as exc:
            # A parser the deployment chose not to install is a configuration
            # problem, not a bad request — say which one and how to fix it.
            raise HTTPException(status_code=501, detail=str(exc))
        except Exception as exc:
            logger.error("Document parse failed", filename=filename, error=str(exc))
            raise HTTPException(status_code=400, detail=f"Could not read the document: {exc}")

        session.add_document(document)
        logger.info("Context document attached", document=filename, chunks=len(document.chunks), session=session.id)
        response.headers[SESSION_HEADER] = session.id

        return DocumentUploadResponse(
            message="Document attached.",
            document=DocumentSummary(**document.summary()),
            session_id=session.id,
        )
    finally:
        cleanup_path(temp_path)


@router.delete("/documents/{name}", dependencies=[Depends(require_api_key)])
async def delete_document(name: str, session: Session = Depends(get_session)) -> dict:
    if not session.remove_document(os.path.basename(name)):
        raise HTTPException(status_code=404, detail=f"No document named '{name}' in this session.")
    return {"message": f"Removed '{name}'."}


@router.post(
    "/datasets/{name}/activate",
    response_model=SessionResponse,
    dependencies=[Depends(require_api_key)],
)
async def activate_dataset(name: str, session: Session = Depends(get_session)) -> SessionResponse:
    """Switches which loaded table is bound to `df` in the sandbox."""
    if not session.set_active(os.path.basename(name)):
        raise HTTPException(status_code=404, detail=f"No dataset named '{name}' in this session.")
    return SessionResponse(**session.describe())


@router.delete("/datasets/{name}", dependencies=[Depends(require_api_key)])
async def delete_dataset(name: str, session: Session = Depends(get_session)) -> dict:
    if not session.remove_dataset(os.path.basename(name)):
        raise HTTPException(status_code=404, detail=f"No dataset named '{name}' in this session.")
    return {"message": f"Removed '{name}'.", "active_dataset": session.active_dataset}


@router.get("/data/preview", response_model=PreviewResponse)
async def preview_data(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=500),
    sort_by: str | None = None,
    sort_order: str = Query(default="asc", pattern="^(asc|desc)$"),
    dataset: str | None = None,
    session: Session = Depends(require_dataset),
) -> PreviewResponse:
    """Paginated view of a loaded table."""
    handle = session.datasets.get(dataset) if dataset else session.active_handle
    if handle is None:
        raise HTTPException(status_code=404, detail="Requested dataset is not loaded in this session.")

    df = handle.df
    if sort_by:
        if sort_by not in df.columns:
            raise HTTPException(status_code=400, detail=f"Unknown column '{sort_by}'.")
        df = df.sort_values(by=sort_by, ascending=sort_order == "asc", kind="stable")

    total_rows = len(df)
    start = (page - 1) * per_page
    subset = df.iloc[start : start + per_page]

    return PreviewResponse(
        page=page,
        per_page=per_page,
        total_rows=total_rows,
        total_pages=max(1, math.ceil(total_rows / per_page)),
        columns=[str(column) for column in df.columns],
        # NaN and +/-Inf are not representable in JSON; the previous implementation
        # relied on df.replace and could still emit them, returning a 500.
        data=json_safe_records(subset),
    )
