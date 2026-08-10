"""Connecting a session to a data source that is not a file."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Response

from src.api.deps import SESSION_HEADER, get_session, require_api_key
from src.api.schemas import (
    ConnectionImportRequest,
    ConnectionImportResponse,
    ConnectionListResponse,
    ConnectionRequest,
    ConnectionSchemaResponse,
    ConnectionSummary,
    ConnectionTestResponse,
    ConnectionWriteRequest,
    ConnectorKindResponse,
    DatasetSummary,
    WriteBackRequest,
)
from src.core.connectors import (
    ConnectionSpec,
    Connector,
    ConnectorError,
    DriverMissing,
    available_kinds,
    build,
    kind_by_name,
    split_secret_from_dsn,
)
from src.core.connectors.gate import authorize, require_writable
from src.core.connectors.ingest import import_target
from src.core.connectors.store import connection_store
from src.core.data_mode import normalize
from src.core.session import Session
from src.utils.errors import safe_error_message
from src.utils.logging import logger


router = APIRouter(prefix="/api", tags=["connections"])


def _summary(spec: ConnectionSpec) -> ConnectionSummary:
    entry = kind_by_name(spec.kind)
    return ConnectionSummary(
        id=spec.id,
        name=spec.name,
        kind=spec.kind,
        options=dict(spec.options),
        read_only=spec.read_only,
        created_at=spec.created_at,
        has_secret=bool(connection_store.secret_for(spec)),
        available=entry.available() if entry else False,
        install_hint=entry.install_hint if entry else "",
    )


def _split_options(options: dict, secret: str | None) -> tuple[dict, str | None]:
    """Lifts an inline password out of a pasted DSN before the spec is built.

    Done at the edge so that no spec ever *holds* a credential, rather than
    relying on every later reader to remember to strip one. An explicitly
    supplied ``secret`` wins: someone who filled in both fields meant the one
    they typed into the password box.
    """
    dsn = str(options.get("dsn") or "").strip()
    if not dsn:
        return options, secret
    cleaned, embedded = split_secret_from_dsn(dsn)
    if not embedded:
        return options, secret
    # A new dict, not a mutation of the caller's: whatever reference the caller
    # (or a logging call earlier in the request lifecycle) still holds must not
    # observe the raw DSN turn into a scrubbed one out from under it.
    scrubbed = dict(options)
    scrubbed["dsn"] = cleaned
    return scrubbed, secret if secret is not None else embedded


def _require_spec(connection_id: str) -> ConnectionSpec:
    spec = connection_store.get(connection_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No connection with id {connection_id!r}.")
    return spec


def _open(spec: ConnectionSpec):
    """Builds a connector, turning a missing driver into a 501 with the remedy.

    501 rather than 500 because the server is working and the feature is simply
    not installed -- the same code the document upload route returns when pypdf
    is absent.
    """
    try:
        return build(spec, connection_store.secret_for(spec))
    except DriverMissing as exc:
        raise HTTPException(status_code=501, detail=f"{exc.message} {exc.detail}")
    except ConnectorError as exc:
        # `exc.detail` often wraps `str()` of the underlying driver exception,
        # which can carry hostnames, driver internals or library versions --
        # safe in a log, not in a response body from a service reachable
        # beyond localhost.
        detail = safe_error_message(exc, "Could not open connection", detail=exc.detail, connection=spec.name)
        raise HTTPException(status_code=400, detail=f"{exc.message} {detail}".strip())


@asynccontextmanager
async def open_connector(spec: ConnectionSpec) -> AsyncIterator[Connector]:
    """Builds a connector and guarantees ``close()`` runs, on every exit path.

    The four routes below used to repeat ``connector = _open(spec)`` followed
    by a ``try/finally`` calling ``close()``. Centralising that here means the
    guarantee -- close runs whether the body returns, raises, or the request
    is cancelled -- is enforced once rather than trusted to be copied
    correctly at every call site, including any added later.
    """
    connector = _open(spec)
    try:
        yield connector
    finally:
        await asyncio.to_thread(connector.close)


def _permit(session: Session, category: str, subject: str) -> None:
    """Applies the permission profile to a user-initiated action, or 403s."""
    ruling = authorize(session.permissions, category, subject)
    if not ruling.allowed:
        raise HTTPException(status_code=403, detail=ruling.reason)


def _check_data_mode(session: Session, spec: ConnectionSpec) -> None:
    """Refuses a connection that would leave the machine under `local-only`.

    **The mode outranks the profile**, so this is checked before `_permit`, the
    same ordering `_orient` uses for web search: the mode decides what is
    possible at all, the profile decides what is asked about among what already
    is. There is no consent that would make this allowed, so it is a refusal
    rather than a prompt.

    Without it `local-only` -- whose own description reads "Nothing is sent
    anywhere" -- would happily pull rows from a hosted warehouse.
    """
    if normalize(session.data_mode) == "local-only" and spec.reaches_network():
        raise HTTPException(
            status_code=403,
            detail=(
                f"This session is set to local-only, so '{spec.name}' is unavailable — "
                "it would reach a data source off this machine. Change the data mode to allow it."
            ),
        )


# ------------------------------------------------------------------ #
@router.get("/connections", response_model=ConnectionListResponse)
async def list_connections(session: Session = Depends(get_session)) -> ConnectionListResponse:
    """Every saved connection, plus what this install can actually reach.

    Network-free on purpose: this renders on every page load, so it reports what
    is configured and which drivers are importable, and probes nothing.
    """
    return ConnectionListResponse(
        connections=[_summary(spec) for spec in connection_store.list()],
        kinds=[ConnectorKindResponse(**entry.to_dict()) for entry in available_kinds()],  # type: ignore[arg-type]
    )


@router.post("/connections", response_model=ConnectionSummary, dependencies=[Depends(require_api_key)])
async def create_connection(request: ConnectionRequest, session: Session = Depends(get_session)) -> ConnectionSummary:
    """Saves a connection. Reaches nothing, so it is not permission-gated.

    Storing a hostname is not connecting to it -- the gate is on opening the
    connection, which is where data actually moves. See `connectors/gate.py`.
    """
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="A connection needs a name.")
    if kind_by_name(request.kind) is None:
        known = ", ".join(entry.kind for entry in available_kinds())
        raise HTTPException(status_code=400, detail=f"Unknown connection kind {request.kind!r}. Known kinds: {known}.")
    if connection_store.by_name(name) is not None:
        raise HTTPException(status_code=409, detail=f"A connection named {name!r} already exists.")

    options, secret = _split_options(dict(request.options), request.secret)
    spec = ConnectionSpec(name=name, kind=request.kind, options=options)
    if not connection_store.save(spec, secret=secret):
        raise HTTPException(status_code=500, detail="Could not save the connection.")
    return _summary(spec)


@router.put("/connections/{connection_id}", response_model=ConnectionSummary, dependencies=[Depends(require_api_key)])
async def update_connection(
    connection_id: str, request: ConnectionRequest, session: Session = Depends(get_session)
) -> ConnectionSummary:
    """Edits a saved connection in place.

    In place rather than delete-and-recreate, which is what correcting a typo'd
    port would otherwise cost: recreating drops the stored secret, every table
    imported from it, the per-source data policy set on it, and the write-back
    opt-in. Editing keeps the id, so none of those are orphaned.

    ``secret`` omitted means "leave the stored one alone" -- an edit that did not
    retype the password must not erase it. ``read_only`` is deliberately *not*
    editable here: it has its own route with its own confirmation.
    """
    spec = _require_spec(connection_id)
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="A connection needs a name.")
    if kind_by_name(request.kind) is None:
        raise HTTPException(status_code=400, detail=f"Unknown connection kind {request.kind!r}.")

    clash = connection_store.by_name(name)
    if clash is not None and clash.id != connection_id:
        raise HTTPException(status_code=409, detail=f"A connection named {name!r} already exists.")

    options, secret = _split_options(dict(request.options), request.secret)
    updated = ConnectionSpec(
        name=name,
        kind=request.kind,
        options=options,
        id=spec.id,
        read_only=spec.read_only,
        created_at=spec.created_at,
    )
    if not connection_store.save(updated, secret=secret):
        raise HTTPException(status_code=500, detail="Could not save the connection.")

    if name != spec.name:
        # `DatasetHandle.origin` and `DataPolicy.per_dataset` are both keyed by
        # name, not id -- a table imported before the rename, and any policy
        # override set on the connection, would otherwise keep pointing at a
        # name nothing is stored under any more. `delete_connection` matches by
        # the *current* name, so without this an already-imported table
        # survives deleting the very connection it says it came from.
        for handle in session.datasets.values():
            if handle.origin == spec.name:
                handle.origin = name
        session.data_policy.rekey(spec.name, name)
    return _summary(updated)


@router.delete("/connections/{connection_id}", dependencies=[Depends(require_api_key)])
async def delete_connection(connection_id: str, session: Session = Depends(get_session)) -> dict[str, str]:
    """Removes a connection, its stored secret, and anything imported from it.

    The imported tables go too. Leaving them would keep rows from a source the
    user just disconnected queryable by generated code, and their data-policy
    overrides would outlive the thing they were set on.
    """
    spec = _require_spec(connection_id)
    for name in [handle.name for handle in session.datasets.values() if handle.origin == spec.name]:
        session.remove_dataset(name)
    session.data_policy.forget(spec.name)
    if not connection_store.delete(connection_id):
        # The datasets are already gone, so reporting success here would leave the
        # user believing a connection they can still see was removed.
        raise HTTPException(status_code=500, detail="Could not remove the connection.")
    return {"message": f"Removed the connection {spec.name!r}."}


@router.post(
    "/connections/{connection_id}/test",
    response_model=ConnectionTestResponse,
    dependencies=[Depends(require_api_key)],
)
async def test_connection(connection_id: str, session: Session = Depends(get_session)) -> ConnectionTestResponse:
    """Reaches the source and reports what happened.

    Not gated, and it never raises for a refused connection: this is the
    diagnostic someone runs *while* typing a hostname, and a diagnostic that
    fails instead of reporting is useless at exactly the moment it is needed.
    """
    spec = _require_spec(connection_id)
    _check_data_mode(session, spec)
    # A test *is* a connect -- it opens a socket to the source -- so the profile
    # binds here too. A `deny` ruling stops it; an `ask` is answered by the click.
    _permit(session, "db_connect", spec.id)
    async with open_connector(spec) as connector:
        try:
            await asyncio.to_thread(connector.test)
        except ConnectorError as exc:
            return ConnectionTestResponse(ok=False, detail=f"{exc.message} {exc.detail}".strip())
        except Exception as exc:
            logger.warning("A connection test failed", connection=spec.name, error=str(exc))
            return ConnectionTestResponse(ok=False, detail=str(exc))
    return ConnectionTestResponse(ok=True, detail="Reached the source.")


@router.post(
    "/connections/{connection_id}/schema",
    response_model=ConnectionSchemaResponse,
    dependencies=[Depends(require_api_key)],
)
async def discover_connection(connection_id: str, session: Session = Depends(get_session)) -> ConnectionSchemaResponse:
    """Lists what the source contains. Gated: metadata leaves the source."""
    spec = _require_spec(connection_id)
    _check_data_mode(session, spec)
    _permit(session, "db_connect", spec.id)
    async with open_connector(spec) as connector:
        try:
            schema = await asyncio.to_thread(connector.discover)
        except ConnectorError as exc:
            raise HTTPException(status_code=400, detail=f"{exc.message} {exc.detail}".strip())
    return ConnectionSchemaResponse(**schema.to_dict())  # type: ignore[arg-type]


@router.post(
    "/connections/{connection_id}/import",
    response_model=ConnectionImportResponse,
    dependencies=[Depends(require_api_key)],
)
async def import_from_connection(
    connection_id: str,
    request: ConnectionImportRequest,
    response: Response,
    session: Session = Depends(get_session),
) -> ConnectionImportResponse:
    """Reads one table into the session, exactly as an upload would.

    Gated: this is the moment rows enter the analysis and become reachable by
    generated code and by a cloud-bound prompt.
    """
    spec = _require_spec(connection_id)
    _check_data_mode(session, spec)
    _permit(session, "db_connect", spec.id)
    target = (request.target or "").strip()
    if not target:
        raise HTTPException(status_code=422, detail="Name the table to import.")

    async with open_connector(spec) as connector:
        try:
            result = await asyncio.to_thread(import_target, session, spec, connector, target, request.make_active)
        except ConnectorError as exc:
            raise HTTPException(status_code=400, detail=f"{exc.message} {exc.detail}".strip())
        except Exception as exc:
            detail = safe_error_message(exc, "A connection import failed", connection=spec.name, target=target)
            raise HTTPException(status_code=400, detail=f"Could not import {target!r}: {detail}")

    response.headers[SESSION_HEADER] = session.id
    return ConnectionImportResponse(
        message=result.message,
        dataset=DatasetSummary(**result.handle.summary()),
        truncated=result.truncated,
        session_id=session.id,
    )


@router.post(
    "/connections/{connection_id}/write",
    response_model=ConnectionTestResponse,
    dependencies=[Depends(require_api_key)],
)
async def write_to_connection(
    connection_id: str, request: ConnectionWriteRequest, session: Session = Depends(get_session)
) -> ConnectionTestResponse:
    """Writes one of this session's tables back to the source.

    Three independent locks, and all three have to be open:

    1. ``spec.read_only`` must have been turned off for *this* connection, once,
       with the name typed back. Checked first, and **without asking anything** --
       a question whose only permitted answer is no is worse than no question.
    2. The ``db_write`` category must not be set to deny. It carries
       ``always_ask``, so no profile can pre-approve it either.
    3. The grant is recorded per ``connection:table``, not per connection:
       approving a write to ``staging.results`` is not approving one to
       ``prod.orders``.
    """
    spec = _require_spec(connection_id)
    _check_data_mode(session, spec)
    writable = require_writable(spec)
    if not writable.allowed:
        raise HTTPException(status_code=403, detail=writable.reason)

    handle = session.datasets.get(request.dataset)
    if handle is None:
        raise HTTPException(status_code=404, detail=f"No dataset named {request.dataset!r} in this session.")

    target = (request.target or "").strip()
    if not target:
        raise HTTPException(status_code=422, detail="Name the table to write to.")
    _permit(session, "db_write", f"{spec.id}:{target}")

    async with open_connector(spec) as connector:
        try:
            await asyncio.to_thread(connector.write, target, handle.df)
        except ConnectorError as exc:
            raise HTTPException(status_code=400, detail=f"{exc.message} {exc.detail}".strip())
        except Exception as exc:
            detail = safe_error_message(exc, "A write-back failed", connection=spec.name, target=target)
            raise HTTPException(status_code=400, detail=f"Could not write to {target!r}: {detail}")

    logger.info("Wrote a table back to a source", connection=spec.name, target=target, rows=len(handle.df))
    return ConnectionTestResponse(ok=True, detail=f"Wrote {len(handle.df):,} rows to '{target}'.")


@router.post(
    "/connections/{connection_id}/write-back",
    response_model=ConnectionSummary,
    dependencies=[Depends(require_api_key)],
)
async def set_write_back(
    connection_id: str, request: WriteBackRequest, session: Session = Depends(get_session)
) -> ConnectionSummary:
    """Turns write-back on or off for one connection.

    Enabling requires the connection's name typed back. This is the one decision
    in the app whose consequences land outside this machine, and the spec is
    explicit that it is made once, deliberately, per connection -- never by a
    permission profile, which `db_write`'s `always_ask` already guarantees.

    Enabling does **not** grant a write. It says this connection *may* be written
    to at all; every session still asks the first time the agent actually writes.
    """
    spec = _require_spec(connection_id)
    if request.enable and (request.confirm or "").strip() != spec.name:
        raise HTTPException(
            status_code=400,
            detail=f"Type the connection's name ({spec.name!r}) to confirm enabling write-back.",
        )

    updated = ConnectionSpec(
        name=spec.name,
        kind=spec.kind,
        options=dict(spec.options),
        id=spec.id,
        read_only=not request.enable,
        created_at=spec.created_at,
    )
    if not connection_store.save(updated, secret=None):
        raise HTTPException(status_code=500, detail="Could not update the connection.")
    logger.info("Changed write-back for a connection", connection=spec.name, write_back=request.enable)
    return _summary(updated)


__all__ = ["router"]
