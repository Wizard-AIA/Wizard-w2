"""MongoDB, and anything speaking its wire protocol.

The document half of the reference set. It exists to prove the interface holds
for a store with no fixed schema, which is where a connector design usually
breaks: ``discover`` cannot read a column list out of a catalog because there
isn't one, so it infers the shape from a bounded sample of documents and says so.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.config import settings

from .base import DEFAULT_SAMPLE_ROWS, refuse_write
from .registry import ConnectorKind, register
from .spec import (
    ColumnInfo,
    ConnectionSchema,
    ConnectionSpec,
    ConnectorError,
    DriverMissing,
    TargetInfo,
    inject_secret_into_dsn,
)


#: How many documents to look at when inferring a collection's fields. A schema
#: guessed from one document is wrong for any collection whose members differ,
#: which is most of them; reading the whole collection to describe it defeats the
#: point of discovery being cheap.
SHAPE_SAMPLE = 50


def _pymongo() -> Any:
    try:
        import pymongo
    except ImportError as exc:
        raise DriverMissing("document", "pymongo") from exc
    return pymongo


class DocumentConnector:
    """A MongoDB database wearing the ``Connector`` interface."""

    def __init__(self, spec: ConnectionSpec, secret: str = ""):
        self.spec = spec
        self._secret = secret
        self._client: Any = None

    # ------------------------------------------------------------------ #
    def _uri(self) -> str:
        options = self.spec.options
        dsn = str(options.get("dsn") or "").strip()
        if dsn:
            # The password was lifted out on save; put it back. See `spec.py`.
            return inject_secret_into_dsn(dsn, self._secret)
        host = str(options.get("host") or "").strip()
        if not host:
            raise ConnectorError(
                "This connection has no host or DSN.",
                detail="Give a host, or a full mongodb:// connection string.",
            )
        port = str(options.get("port") or "").strip()
        return f"mongodb://{host}:{port}" if port else f"mongodb://{host}"

    def _collection_name(self, target: str) -> str:
        """The collection a target names, stripping only a leading ``database.``.

        MongoDB permits dots in a collection name, and ``discover`` already
        returns the bare name with the database in ``namespace`` -- so splitting
        on the *last* dot never removed a prefix that was there and did truncate
        a legitimate name: ``logs.2024`` became ``2024``, which reads an empty
        collection rather than failing. One helper, used by every method that
        resolves a target, so the two cannot drift.
        """
        name = (target or "").strip()
        prefix = f"{self._database_name()}."
        return name[len(prefix) :] if name.startswith(prefix) else name

    def _database_name(self) -> str:
        name = str(self.spec.options.get("database") or "").strip()
        if not name:
            raise ConnectorError(
                "This connection names no database.",
                detail="Set the database this connection should read from.",
            )
        return name

    def _connect(self) -> Any:
        if self._client is None:
            pymongo = _pymongo()
            options = self.spec.options
            user = str(options.get("user") or "").strip()
            try:
                # A short server-selection timeout, because the default is 30
                # seconds: an unreachable host has to fail while somebody is
                # still looking at the screen, not appear to hang.
                self._client = pymongo.MongoClient(
                    self._uri(),
                    username=user or None,
                    password=self._secret or None,
                    serverSelectionTimeoutMS=int(settings.CONNECTOR_TIMEOUT) * 1000,
                    connectTimeoutMS=int(settings.CONNECTOR_TIMEOUT) * 1000,
                )
            except Exception as exc:
                raise ConnectorError("Could not open the connection.", detail=str(exc)) from exc
        return self._client

    # ------------------------------------------------------------------ #
    def test(self) -> None:
        client = self._connect()
        try:
            client.admin.command("ping")
        except Exception as exc:
            raise ConnectorError("Could not reach the database.", detail=str(exc)) from exc

    def discover(self) -> ConnectionSchema:
        client = self._connect()
        database = self._database_name()
        try:
            targets: list[TargetInfo] = []
            for name in client[database].list_collection_names():
                collection = client[database][name]
                fields: dict[str, str] = {}
                for document in collection.find(limit=SHAPE_SAMPLE):
                    for key, value in document.items():
                        fields.setdefault(str(key), type(value).__name__)
                targets.append(
                    TargetInfo(
                        name=name,
                        namespace=database,
                        columns=[ColumnInfo(name=key, type=fields[key]) for key in sorted(fields)],
                        row_estimate=collection.estimated_document_count(),
                    )
                )
        except Exception as exc:
            raise ConnectorError("Could not read the collections.", detail=str(exc)) from exc
        return ConnectionSchema(targets=targets)

    def sample(self, target: str, limit: int = DEFAULT_SAMPLE_ROWS) -> pd.DataFrame:
        client = self._connect()
        database = self._database_name()
        collection_name = self._collection_name(target)
        try:
            documents = list(client[database][collection_name].find(limit=int(limit)))
        except Exception as exc:
            raise ConnectorError(f"Could not read '{target}'.", detail=str(exc)) from exc
        return self._frame(documents)

    def fetch(self, query: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """Runs a JSON find specification: ``{"collection": ..., "filter": {...}}``.

        A find document rather than a query language, because Mongo has no single
        textual one and inventing a dialect here would be a second thing to learn
        that only Wizard speaks. ``params`` is part of the shared ``Connector``
        signature; Mongo's filter document has no separate bind-parameter slot,
        so there is nothing here for it to do.
        """
        import json

        client = self._connect()
        database = self._database_name()
        try:
            request = json.loads(query)
        except ValueError as exc:
            raise ConnectorError("The query is not valid JSON.", detail=str(exc)) from exc
        if not isinstance(request, dict) or not request.get("collection"):
            raise ConnectorError(
                "The query names no collection.",
                detail='Expected {"collection": "name", "filter": {...}, "limit": n}.',
            )
        try:
            cursor = client[database][str(request["collection"])].find(
                request.get("filter") or {},
                limit=int(request.get("limit") or DEFAULT_SAMPLE_ROWS),
            )
            documents = list(cursor)
        except Exception as exc:
            raise ConnectorError("The query failed.", detail=str(exc)) from exc
        return self._frame(documents)

    def write(self, target: str, df: pd.DataFrame) -> None:
        refuse_write(self.spec)
        client = self._connect()
        database = self._database_name()
        collection_name = self._collection_name(target)
        records = df.to_dict(orient="records")
        if not records:
            return
        try:
            client[database][collection_name].insert_many(records)
        except Exception as exc:
            raise ConnectorError(f"Could not write to '{target}'.", detail=str(exc)) from exc

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _frame(documents: list[dict[str, Any]]) -> pd.DataFrame:
        """Flattens documents into a frame, stringifying the ObjectId.

        ``_id`` is a BSON ObjectId, which survives neither Feather transport nor
        anything the analysis toolkit does with it. Left alone it fails at
        materialisation -- after the read, when the useful error is long gone.
        """
        frame = pd.DataFrame(documents)
        if "_id" in frame.columns:
            frame["_id"] = frame["_id"].astype(str)
        return frame


register(
    ConnectorKind(
        kind="document",
        label="MongoDB",
        factory=DocumentConnector,
        module="pymongo",
        distribution="pymongo",
        fields=("host", "port", "database", "user", "dsn"),
        requires_secret=False,
        description="MongoDB and compatible document stores. Collections are read as tables.",
    )
)


__all__ = ["DocumentConnector"]
