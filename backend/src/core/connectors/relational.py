"""Anything SQLAlchemy has a dialect for.

One module covers Postgres, MySQL, SQL Server, Oracle, Snowflake, BigQuery and
the rest, because SQLAlchemy's dialect system is the abstraction this milestone
was told to build on rather than reinvent. Which engines an install can reach is
therefore a question about which dialect packages are installed, not about this
file -- exactly the "no hardcoded connector whitelist" the spec asks for.

SQLite is the reason this is testable. It needs no third-party driver at all, so
the suite exercises the *real* connector against a real database with nothing
running and no network.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

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


#: Which ``connect_args`` key each dialect family spells its connect timeout with.
#: SQLAlchemy has no dialect-agnostic one -- the timeout belongs to the DBAPI
#: driver, not to the engine -- so it is applied where it is known and honestly
#: skipped where it is not, rather than passed blindly and rejected at connect.
#: SQLite is deliberately absent: it opens a local file and has nothing to dial.
CONNECT_TIMEOUT_ARG: tuple[tuple[str, str], ...] = (
    ("postgresql", "connect_timeout"),
    ("mysql", "connect_timeout"),
    ("mariadb", "connect_timeout"),
    ("mssql", "timeout"),
    ("oracle", "tcp_connect_timeout"),
    ("snowflake", "login_timeout"),
)


def _sqlalchemy() -> Any:
    """Imports SQLAlchemy, or reports how to get it.

    Imported here rather than at module scope so this module imports on a machine
    without SQLAlchemy -- the registry still lists the kind, marked unavailable
    with the command that fixes it.
    """
    try:
        import sqlalchemy
    except ImportError as exc:
        raise DriverMissing("relational", "SQLAlchemy") from exc
    return sqlalchemy


class RelationalConnector:
    """A SQLAlchemy engine wearing the ``Connector`` interface."""

    def __init__(self, spec: ConnectionSpec, secret: str = ""):
        self.spec = spec
        self._secret = secret
        self._engine: Any = None

    # ------------------------------------------------------------------ #
    def _url(self) -> str:
        """Builds the SQLAlchemy URL from the non-secret options plus the secret.

        The password is injected here and nowhere else, which is what keeps it
        out of ``spec.options`` -- and therefore out of ``connections.json``, out
        of every API response, and out of the logs.
        """
        options = self.spec.options
        dsn = str(options.get("dsn") or "").strip()
        if dsn:
            # A full DSN is taken as given, except for the password: it was lifted
            # out on save so the spec holds no secret, and is put back here. A DSN
            # that never carried one is returned untouched.
            return inject_secret_into_dsn(dsn, self._secret)

        driver = str(options.get("driver") or "").strip()
        if not driver:
            raise ConnectorError(
                "This connection has no driver or DSN.",
                detail="Give a SQLAlchemy driver (for example `postgresql+psycopg`) or a full DSN.",
            )

        database = str(options.get("database") or "").strip()
        host = str(options.get("host") or "").strip()
        if not host:
            # SQLite and other file-backed engines: no host, no credentials, the
            # database is a path.
            return f"{driver}:///{database}"

        user = str(options.get("user") or "").strip()
        port = str(options.get("port") or "").strip()
        auth = ""
        if user:
            auth = quote_plus(user)
            if self._secret:
                auth = f"{auth}:{quote_plus(self._secret)}"
            auth = f"{auth}@"
        location = f"{host}:{port}" if port else host
        return f"{driver}://{auth}{location}/{database}"

    def _connect_args(self, url: str) -> dict[str, Any]:
        """A connect timeout for the dialects that accept one.

        Without it an unreachable host waits out the driver's own default --
        30 seconds or more for several of them -- which reads as a hang rather
        than as a wrong hostname.
        """
        scheme = url.split("://", 1)[0].split("+", 1)[0].lower()
        for family, argument in CONNECT_TIMEOUT_ARG:
            if scheme == family:
                return {argument: int(settings.CONNECTOR_TIMEOUT)}
        return {}

    def _connect(self) -> Any:
        if self._engine is None:
            sqlalchemy = _sqlalchemy()
            # Built before the try, so a spec problem keeps its own message. Folded
            # into the generic handler below it became "could not open the
            # connection", which names neither the cause nor the fix.
            url = self._url()
            try:
                self._engine = sqlalchemy.create_engine(url, pool_pre_ping=True, connect_args=self._connect_args(url))
            except Exception as exc:
                raise ConnectorError("Could not open the connection.", detail=str(exc)) from exc
        return self._engine

    # ------------------------------------------------------------------ #
    def test(self) -> None:
        sqlalchemy = _sqlalchemy()
        engine = self._connect()
        try:
            with engine.connect() as connection:
                connection.execute(sqlalchemy.text("SELECT 1"))
        except Exception as exc:
            raise ConnectorError("Could not reach the database.", detail=str(exc)) from exc

    def discover(self) -> ConnectionSchema:
        sqlalchemy = _sqlalchemy()
        engine = self._connect()
        try:
            inspector = sqlalchemy.inspect(engine)
            targets: list[TargetInfo] = []
            # Views as well as tables: a warehouse usually presents its useful
            # shapes as views, and listing only tables would hide them.
            for schema in inspector.get_schema_names() or [None]:
                names = list(inspector.get_table_names(schema=schema))
                names += list(inspector.get_view_names(schema=schema))
                for name in names:
                    columns = [
                        ColumnInfo(name=str(column["name"]), type=str(column.get("type", "")))
                        for column in inspector.get_columns(name, schema=schema)
                    ]
                    targets.append(TargetInfo(name=name, namespace=schema or "", columns=columns))
        except Exception as exc:
            raise ConnectorError("Could not read the database schema.", detail=str(exc)) from exc
        return ConnectionSchema(targets=targets)

    def sample(self, target: str, limit: int = DEFAULT_SAMPLE_ROWS) -> pd.DataFrame:
        sqlalchemy = _sqlalchemy()
        engine = self._connect()
        # Reflected and built as a `select().limit()` rather than assembled into a
        # SQL string. Two reasons, both correctness: the row limit is spelled
        # differently by dialect (`LIMIT`, `TOP`, `FETCH FIRST`) and only the
        # dialect knows which, and reflection means the identifier is never
        # interpolated -- which matters because a table name reaches a query even
        # though it came from discovery rather than from a user.
        namespace, _, bare = target.rpartition(".")
        try:
            metadata = sqlalchemy.MetaData()
            table = sqlalchemy.Table(bare, metadata, schema=namespace or None, autoload_with=engine)
            statement = sqlalchemy.select(table).limit(int(limit))
            with engine.connect() as connection:
                return pd.read_sql(statement, connection)
        except Exception as exc:
            raise ConnectorError(f"Could not read '{target}'.", detail=str(exc)) from exc

    def fetch(self, query: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """Runs ``query`` with ``:name``-style bind parameters, never string interpolation.

        ``params`` goes to the driver as bound values -- the only way a caller
        can vary a query by a value without that value being parsed as SQL. A
        caller that instead formats the value into ``query`` gets the same
        injection ``text()`` was supposed to prevent; this signature exists so
        that caller has a safe alternative to reach for.
        """
        sqlalchemy = _sqlalchemy()
        engine = self._connect()
        try:
            with engine.connect() as connection:
                return pd.read_sql(sqlalchemy.text(query), connection, params=params)
        except Exception as exc:
            raise ConnectorError("The query failed.", detail=str(exc)) from exc

    def write(self, target: str, df: pd.DataFrame) -> None:
        refuse_write(self.spec)
        engine = self._connect()
        namespace, _, bare = target.rpartition(".")
        try:
            with engine.begin() as connection:
                df.to_sql(bare, connection, schema=namespace or None, if_exists="append", index=False)
        except Exception as exc:
            raise ConnectorError(f"Could not write to '{target}'.", detail=str(exc)) from exc

    def close(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            engine.dispose()


register(
    ConnectorKind(
        kind="relational",
        label="SQL database",
        factory=RelationalConnector,
        module="sqlalchemy",
        distribution="SQLAlchemy",
        fields=("driver", "host", "port", "database", "user", "dsn"),
        requires_secret=False,
        description=(
            "Any engine SQLAlchemy has a dialect for -- PostgreSQL, MySQL, SQL Server, "
            "Snowflake, SQLite and others. Install the dialect's own driver package too."
        ),
    )
)


__all__ = ["RelationalConnector"]
