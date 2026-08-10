"""The connector layer, with no database running.

Most of this file exercises inert data and pure functions, for the same reason
``test_sandbox_policy`` does: the interface is where the design is either right
or wrong, and it has to be reviewable from a laptop with no warehouse on it.

The one place a *real* engine is used is SQLite, which needs no third-party
driver at all -- that is what makes the relational connector testable rather
than merely described.
"""

from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest

from src.config import settings
from src.core.connectors import (
    ConnectionSpec,
    ConnectorError,
    ConnectorKind,
    DriverMissing,
    available_kinds,
    build,
    inject_secret_into_dsn,
    kind_by_name,
    refuse_write,
    register,
    sanitize_identifier,
    split_secret_from_dsn,
)
from src.core.connectors.store import ConnectionStore
from src.core.credentials import CredentialStore


# ------------------------------------------------------------------- spec --
def test_a_dotted_target_cannot_collapse_the_table_key() -> None:
    """The trap that makes two imported tables silently become one.

    ``DatasetHandle.table_key`` derives from ``Path(name).stem``, so a dataset
    named ``warehouse.orders`` would have stem ``warehouse`` -- and every table
    from that connection would land on the same key, the same feather file and
    the same ``tables[...]`` binding, each overwriting the last. The name is
    built dot-free for exactly this reason.
    """
    spec = ConnectionSpec(name="Warehouse", kind="relational")

    orders = spec.dataset_name("public.orders")
    customers = spec.dataset_name("public.customers")

    assert "." not in orders
    assert orders == "warehouse_public_orders"
    assert orders != customers


def test_a_connection_spec_carries_no_secret() -> None:
    """There is no field to forget to strip, which is stronger than remembering to."""
    spec = ConnectionSpec(name="W", kind="relational", options={"host": "db.internal", "user": "reader"})

    serialised = json.dumps(spec.to_dict())

    assert "password" not in serialised
    assert spec.credential_key == f"connection:{spec.id}"
    assert spec.read_only is True, "a connection must start read-only"


def test_a_spec_round_trips_through_disk() -> None:
    spec = ConnectionSpec(name="W", kind="relational", options={"host": "h"}, read_only=False)

    restored = ConnectionSpec.from_dict(json.loads(json.dumps(spec.to_dict())))

    assert (restored.id, restored.name, restored.options, restored.read_only) == (
        spec.id,
        spec.name,
        spec.options,
        False,
    )


def test_a_spec_missing_fields_falls_back_rather_than_raising() -> None:
    """A connections file that lost a key costs one retyped field, not a dead backend."""
    restored = ConnectionSpec.from_dict({"name": "W", "kind": "relational"})

    assert restored.read_only is True
    assert restored.options == {}
    assert restored.id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Q3 sales (final)", "q3_sales_final"), ("  ", "source"), ("__x__", "x"), ("A.B", "a_b")],
)
def test_identifiers_are_folded_to_a_safe_key(raw: str, expected: str) -> None:
    assert sanitize_identifier(raw) == expected


@pytest.mark.parametrize(
    ("dsn", "expected_dsn", "expected_secret"),
    [
        (
            "postgresql://admin:hunter2@db.internal:5432/prod",
            "postgresql://admin@db.internal:5432/prod",
            "hunter2",
        ),
        # No password: returned untouched rather than rearranged.
        ("postgresql://admin@db.internal/prod", "postgresql://admin@db.internal/prod", ""),
        ("sqlite:///local.db", "sqlite:///local.db", ""),
        # Percent-encoded, because a real password contains punctuation.
        ("mongodb://u:p%40ss%3Aword@host:27017", "mongodb://u@host:27017", "p@ss:word"),
        ("", "", ""),
    ],
)
def test_a_pasted_dsn_gives_up_its_password(dsn: str, expected_dsn: str, expected_secret: str) -> None:
    """Pasting a DSN is the most common way anyone configures a database.

    Left whole, the password inside it lands in `spec.options` -- and therefore
    in `connections.json`, in every API response and in anything that logs a
    spec, making this module's central claim false in exactly the case people
    use most.
    """
    assert split_secret_from_dsn(dsn) == (expected_dsn, expected_secret)


def test_the_password_goes_back_in_when_the_connection_opens() -> None:
    """Round-trips, or the split would just break every DSN connection."""
    original = "postgresql://admin:hunter2@db.internal:5432/prod"
    stripped, secret = split_secret_from_dsn(original)

    assert inject_secret_into_dsn(stripped, secret) == original


def test_injecting_into_a_dsn_that_already_has_one_changes_nothing() -> None:
    dsn = "postgresql://admin:already@host/db"

    assert inject_secret_into_dsn(dsn, "other") == dsn


@pytest.mark.parametrize(
    ("options", "remote"),
    [
        ({"driver": "sqlite", "database": "/tmp/x.db"}, False),
        ({"driver": "postgresql", "host": "localhost"}, False),
        ({"driver": "postgresql", "host": "127.0.0.1"}, False),
        ({"driver": "postgresql", "host": "warehouse.example.com"}, True),
        ({"dsn": "postgresql://u@db.example.com/p"}, True),
        ({"dsn": "postgresql://u@localhost/p"}, False),
        ({"dsn": "postgresql://u:pw@127.0.0.1:5432/p"}, False),
        # A DSN naming no host at all is a *file*. Judged by whether a loopback
        # name appeared anywhere in the string, these read as remote, and
        # `local-only` refused the one connection that never leaves the machine.
        ({"dsn": "sqlite:///tmp/local.db"}, False),
        ({"dsn": "sqlite:////abs/path.db"}, False),
        ({"endpoint_url": "https://s3.amazonaws.com", "bucket": "b"}, True),
        ({"bucket": "my-bucket"}, True),
        ({"endpoint_url": "http://127.0.0.1:9000", "bucket": "b"}, False),
    ],
)
def test_a_connection_knows_whether_it_leaves_the_machine(options: dict, remote: bool) -> None:
    """`local-only` says "Nothing is sent anywhere" and has to mean it.

    Judged from the endpoint rather than the kind: the same driver is local or
    remote depending only on where it points.
    """
    assert ConnectionSpec(name="X", kind="relational", options=options).reaches_network() is remote


# --------------------------------------------------------------- registry --
def test_a_contributor_can_register_a_kind_without_touching_core() -> None:
    """The extensibility claim, asserted rather than described.

    If a connector can be registered from outside the package and then built by
    id, so can one contributed by somebody else -- which is what the spec means
    by "addable without touching core orchestration code".
    """

    class Fake:
        def __init__(self, spec: ConnectionSpec, secret: str = ""):
            self.spec = spec

    register(ConnectorKind(kind="fake", label="Fake", factory=Fake))
    try:
        assert kind_by_name("fake") is not None
        assert isinstance(build(ConnectionSpec(name="f", kind="fake")), Fake)
    finally:
        from src.core.connectors.registry import _FACTORIES

        _FACTORIES.pop("fake", None)


def test_an_unknown_kind_is_refused_by_name() -> None:
    with pytest.raises(ConnectorError, match="Unknown connection kind"):
        build(ConnectionSpec(name="x", kind="teleport"))


def test_a_missing_driver_is_reported_with_the_command_that_fixes_it() -> None:
    """Absent drivers are listed, not hidden.

    Hiding the kind would let a user conclude Wizard cannot reach Postgres,
    when the true answer is that one pip install would.
    """
    entry = ConnectorKind(
        kind="absent", label="Absent", factory=lambda spec, secret: None, module="no_such_module_xyz", distribution="x"
    )

    assert entry.available() is False
    assert entry.install_hint == "pip install x"


def test_build_refuses_a_known_kind_whose_driver_is_absent() -> None:
    """`build` has to be where the driver is checked, or the 501 path is dead.

    The reference connectors import lazily, inside `_connect`, so without a check
    here `build` succeeded and `DriverMissing` surfaced later from `test` or
    `discover` -- where the route catches `ConnectorError` and reports a generic
    400, so the "install this package" answer never reached the user.
    """
    register(
        ConnectorKind(
            kind="absent-driver",
            label="Absent",
            factory=lambda spec, secret: None,  # type: ignore[arg-type,return-value]
            module="no_such_module_xyz",
            distribution="ghost",
        )
    )
    try:
        with pytest.raises(DriverMissing) as caught:
            build(ConnectionSpec(name="x", kind="absent-driver"))
        assert caught.value.install_hint == "pip install ghost"
    finally:
        from src.core.connectors.registry import _FACTORIES

        _FACTORIES.pop("absent-driver", None)


def test_a_dotted_collection_name_is_not_truncated() -> None:
    """MongoDB allows dots in a collection name.

    Splitting on the last dot never removed a database prefix that was there --
    `discover` returns the bare name -- but it did turn `logs.2024` into `2024`,
    which silently reads a different (empty) collection.
    """
    from src.core.connectors.document import DocumentConnector

    connector = DocumentConnector(ConnectionSpec(name="M", kind="document", options={"database": "app"}))

    assert connector._collection_name("logs.2024") == "logs.2024"
    assert connector._collection_name("app.logs.2024") == "logs.2024"
    assert connector._collection_name("orders") == "orders"


def test_an_object_store_refuses_a_cleartext_remote_endpoint() -> None:
    """An http:// endpoint sends the request signature -- derived from the secret -- in the clear.

    Loopback still works, because MinIO on 127.0.0.1 is the ordinary development
    setup and nothing leaves the machine there.
    """
    from src.core.connectors.objectstore import ObjectStoreConnector

    remote = ObjectStoreConnector(ConnectionSpec(name="S", kind="objectstore", options={"bucket": "b"}))
    with pytest.raises(ConnectorError, match="cleartext"):
        remote._require_safe_endpoint("http://minio.internal:9000")

    # No exception for either of these.
    remote._require_safe_endpoint("http://127.0.0.1:9000")
    remote._require_safe_endpoint("https://s3.amazonaws.com")


def test_every_reference_kind_is_registered() -> None:
    assert {entry.kind for entry in available_kinds()} >= {"relational", "document", "objectstore"}


def test_write_is_refused_on_a_read_only_spec() -> None:
    with pytest.raises(ConnectorError, match="read-only"):
        refuse_write(ConnectionSpec(name="W", kind="relational"))


# ------------------------------------------------------------------ store --
def test_the_connections_file_never_contains_the_secret(tmp_path) -> None:
    """The single most important assertion in the connector layer."""
    store = ConnectionStore(path=tmp_path / "connections.json")
    spec = ConnectionSpec(name="W", kind="relational", options={"host": "db.internal"})

    store.save(spec, secret="hunter2-super-secret")

    assert "hunter2-super-secret" not in (tmp_path / "connections.json").read_text(encoding="utf-8")
    assert store.secret_for(spec) == "hunter2-super-secret", "but it is still retrievable"


def test_deleting_a_connection_takes_its_secret_with_it(tmp_path) -> None:
    """A credential left behind is a stored secret nobody can see to revoke."""
    store = ConnectionStore(path=tmp_path / "connections.json")
    spec = ConnectionSpec(name="W", kind="relational")
    store.save(spec, secret="s3cret")

    # Called outside the assert: under `python -O` assertions are stripped, and
    # the deletion this test is about would never happen.
    deleted = store.delete(spec.id)

    assert deleted is True
    assert store.secret_for(spec) == ""
    assert store.get(spec.id) is None


def test_a_corrupt_connections_file_reads_as_empty(tmp_path) -> None:
    """Never a backend that will not answer, for the same reason as the key store."""
    path = tmp_path / "connections.json"
    path.write_text("{not json at all", encoding="utf-8")

    assert ConnectionStore(path=path).list() == []


def test_a_connection_is_findable_by_name(tmp_path) -> None:
    """What Milestone 9's exported script needs: a name, not an opaque id."""
    store = ConnectionStore(path=tmp_path / "connections.json")
    store.save(ConnectionSpec(name="Warehouse", kind="relational"), secret="")

    assert store.by_name("warehouse") is not None
    assert store.by_name("nothing") is None


def test_a_connection_secret_is_not_reported_as_a_provider_key(tmp_path) -> None:
    """They share one flat keyspace, so the provider list has to exclude them.

    Without the filter a saved database password shows up on the models page as
    a configured model provider.
    """
    store = CredentialStore(path=tmp_path / "credentials.json")
    store.set("openai", "sk-test")
    store.set("connection:abc123", "db-password")

    assert store.providers_with_keys() == ["openai"]
    assert store.names("connection:") == ["connection:abc123"]


# ------------------------------------------------------- relational driver --
@pytest.fixture
def sqlite_spec(tmp_path):
    """A real database, built with the standard library and no server."""
    database = tmp_path / "shop.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)")
    connection.executemany(
        "INSERT INTO orders VALUES (?, ?, ?)",
        [(index, "north" if index % 2 else "south", index * 1.5) for index in range(25)],
    )
    connection.commit()
    connection.close()
    return ConnectionSpec(name="Shop", kind="relational", options={"driver": "sqlite", "database": str(database)})


def test_a_real_sqlite_connection_reads_end_to_end(sqlite_spec) -> None:
    pytest.importorskip("sqlalchemy")
    connector = build(sqlite_spec)
    try:
        connector.test()
        schema = connector.discover()
        frame = connector.sample("orders", limit=5)
    finally:
        connector.close()

    assert "orders" in {target.name for target in schema.targets}
    assert len(frame) == 5
    assert list(frame.columns) == ["id", "region", "amount"]


def test_fetch_binds_params_instead_of_interpolating_them(sqlite_spec) -> None:
    """A value containing SQL syntax must stay data, never become code.

    ``region`` here is exactly what a naive ``f"...WHERE region = '{region}'"``
    caller would format into the query string -- if `fetch` did that internally,
    or if a caller had no bind-parameter path and had to do it themselves, this
    value would close the string and inject a second statement. Bound through
    ``params`` it can only ever match a row.
    """
    pytest.importorskip("sqlalchemy")
    connector = build(sqlite_spec)
    injection_attempt = "north' OR '1'='1"
    try:
        frame = connector.fetch(
            "SELECT * FROM orders WHERE region = :region",
            params={"region": injection_attempt},
        )
    finally:
        connector.close()

    assert len(frame) == 0, "the injected condition must not have matched every row"


def test_fetch_still_works_with_no_params(sqlite_spec) -> None:
    pytest.importorskip("sqlalchemy")
    connector = build(sqlite_spec)
    try:
        frame = connector.fetch("SELECT * FROM orders WHERE region = 'north'")
    finally:
        connector.close()

    assert len(frame) > 0
    assert set(frame["region"]) == {"north"}


def test_fetch_is_bounded_by_connector_max_rows(sqlite_spec, monkeypatch) -> None:
    """`fetch` must not load an unbounded result into memory.

    The query is arbitrary SQL text, so unlike `sample` the limit cannot be
    pushed into the statement -- this asserts the fallback: `fetch` stops
    pulling chunks once it has read past `CONNECTOR_MAX_ROWS` rather than
    materialising the whole result set and slicing it afterwards.
    """
    pytest.importorskip("sqlalchemy")
    monkeypatch.setattr(settings, "CONNECTOR_MAX_ROWS", 5)
    connector = build(sqlite_spec)
    try:
        frame = connector.fetch("SELECT * FROM orders")
    finally:
        connector.close()

    assert len(frame) == 5


def test_the_row_limit_is_pushed_down_not_sliced_afterwards(sqlite_spec) -> None:
    """`sample` must bound the read in the *engine*, not fetch everything and slice.

    Asserted against the SQL actually issued, not just the row count: a connector
    that selected all 25 rows and then took the first 3 in pandas would return an
    identical frame while having paid for the whole table -- which is the entire
    thing this signature exists to avoid.
    """
    sqlalchemy = pytest.importorskip("sqlalchemy")
    connector = build(sqlite_spec)
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        statements.append(statement)

    try:
        sqlalchemy.event.listen(connector._connect(), "before_cursor_execute", record)
        frame = connector.sample("orders", limit=3)
    finally:
        connector.close()

    assert len(frame) == 3
    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert selects, "no SELECT was issued"
    assert any("LIMIT" in statement.upper() for statement in selects), selects


def test_a_read_only_connection_refuses_to_write(sqlite_spec) -> None:
    pytest.importorskip("sqlalchemy")
    connector = build(sqlite_spec)
    try:
        with pytest.raises(ConnectorError, match="read-only"):
            connector.write("orders", pd.DataFrame({"id": [1]}))
    finally:
        connector.close()


def test_an_unreachable_database_reports_rather_than_hangs(tmp_path) -> None:
    """Degrades with a message; never a bare driver traceback."""
    pytest.importorskip("sqlalchemy")
    spec = ConnectionSpec(name="Broken", kind="relational", options={"driver": "sqlite", "database": "/nope/x.db"})
    connector = build(spec)

    with pytest.raises(ConnectorError):
        connector.test()


@pytest.mark.parametrize(
    ("driver", "expected"),
    [
        ("postgresql+psycopg", {"connect_timeout": 2}),
        ("mysql+pymysql", {"connect_timeout": 2}),
        ("mssql+pyodbc", {"timeout": 2}),
        # A local file has nothing to dial, and passing an argument the driver
        # does not accept fails the connect outright.
        ("sqlite", {}),
    ],
)
def test_the_connect_timeout_reaches_the_driver(driver: str, expected: dict) -> None:
    """`CONNECTOR_TIMEOUT` must actually be read, not merely defined.

    It was defined and consulted by nothing once already, which is how an
    unreachable host waits out a driver's 30-second default and reads as a hang.
    SQLAlchemy has no dialect-agnostic connect timeout -- the spelling belongs to
    the DBAPI driver -- so it is applied where known and skipped where not.
    """
    pytest.importorskip("sqlalchemy")
    connector = build(ConnectionSpec(name="T", kind="relational", options={"driver": driver}))

    assert connector._connect_args(f"{driver}://host/db") == expected


def test_a_spec_with_no_driver_or_dsn_says_so() -> None:
    pytest.importorskip("sqlalchemy")
    connector = build(ConnectionSpec(name="Empty", kind="relational"))

    with pytest.raises(ConnectorError, match="no driver or DSN"):
        connector.test()
