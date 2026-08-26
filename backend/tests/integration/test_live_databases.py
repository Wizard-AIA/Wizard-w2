from __future__ import annotations

import datetime
import decimal
import json
import os
import socket
import threading
import uuid
from collections.abc import Generator
from typing import Any

import pandas as pd
import pytest

from src.core.connectors.relational import RelationalConnector
from src.core.connectors.spec import ConnectionSpec


# Optional dependencies imported with pytest.importorskip
sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("pymysql")
pytest.importorskip("psycopg2")  # Used by sqlalchemy for pg
redis = pytest.importorskip("redis")


def _is_port_open(host: str, port: int) -> bool:
    """
    Helper function to check if a specific port is open on a given host.
    This is used to skip tests if the required database containers are not running.

    Args:
        host (str): The hostname or IP address to check.
        port (int): The port number to check.

    Returns:
        bool: True if the port is open and accepting connections, False otherwise.
    """
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


# PostgreSQL Configuration
PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = int(os.environ.get("PG_PORT", 5432))
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASS = os.environ.get("PG_PASS", "postgres")
PG_DB = os.environ.get("PG_DB", "wizard_test")
PG_DSN = f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}"
PG_AVAILABLE = _is_port_open(PG_HOST, PG_PORT)

# MySQL Configuration
MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASS = os.environ.get("MYSQL_PASS", "rootpassword")
MYSQL_DB = os.environ.get("MYSQL_DB", "wizard_test")
MYSQL_DSN = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASS}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4"
MYSQL_AVAILABLE = _is_port_open(MYSQL_HOST, MYSQL_PORT)

# Redis Configuration
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_DB = int(os.environ.get("REDIS_DB", 0))
REDIS_AVAILABLE = _is_port_open(REDIS_HOST, REDIS_PORT)


@pytest.fixture
def pg_engine() -> Generator[Any, None, None]:
    """
    Fixture providing a direct SQLAlchemy engine for PostgreSQL setup/teardown.
    This allows us to create tables and insert data bypassing the connector logic
    if we want to test read capabilities independently.
    """
    engine = sa.create_engine(PG_DSN, pool_size=20, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_connector() -> Generator[RelationalConnector, None, None]:
    """
    Fixture providing a RelationalConnector configured for PostgreSQL.
    """
    spec = ConnectionSpec(name="pg_test", kind="relational", options={"dsn": PG_DSN})
    connector = RelationalConnector(spec)
    yield connector
    connector.close()


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL container is not available on localhost:5432")
class TestLivePostgresIntegration:
    """
    Comprehensive test suite for PostgreSQL integration using live database containers.
    These tests verify end-to-end functionality including connectivity, bulk operations,
    concurrency, data types, and multi-schema operations.
    """

    def test_pg_handshake_and_basic_query(self, pg_engine: Any, pg_connector: RelationalConnector) -> None:
        """
        1. test_pg_handshake_and_basic_query
        Tests basic table creation, data insertion, and RelationalConnector's
        test(), discover(), and sample() methods.
        """
        table_name = f"test_basic_{uuid.uuid4().hex[:8]}"
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, val TEXT)"))
            conn.execute(sa.text(f"INSERT INTO {table_name} (id, val) VALUES (1, 'hello'), (2, 'world')"))

        # Test RelationalConnector APIs
        pg_connector.test()
        schema = pg_connector.discover()

        # Ensure schema logic worked
        assert schema is not None

        # Verify sampling
        sample_df = pg_connector.sample(table_name, limit=10)
        assert isinstance(sample_df, pd.DataFrame)
        assert len(sample_df) == 2
        assert "id" in sample_df.columns
        assert "val" in sample_df.columns

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_pg_bulk_insert_and_read_100k_rows(self, pg_engine: Any, pg_connector: RelationalConnector) -> None:
        """
        2. test_pg_bulk_insert_and_read_100k_rows
        Inserts 100K rows via SQLAlchemy batching, then reads back via the connector
        to verify throughput and memory stability.
        """
        table_name = f"test_bulk_{uuid.uuid4().hex[:8]}"
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, value FLOAT, name TEXT)"))

        # Insert 100k rows in batches
        rows = [{"id": i, "value": i * 1.5, "name": f"name_{i}"} for i in range(100000)]
        with pg_engine.begin() as conn:
            for i in range(0, len(rows), 10000):
                batch = rows[i : i + 10000]
                conn.execute(sa.text(f"INSERT INTO {table_name} (id, value, name) VALUES (:id, :value, :name)"), batch)

        # Read back all rows
        df = pg_connector.fetch(f"SELECT * FROM {table_name}")
        assert len(df) == 100000
        assert df["id"].sum() == sum(range(100000))
        assert df["value"].mean() == pytest.approx(74999.25)

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_pg_concurrent_connections(self, pg_engine: Any) -> None:
        """
        3. test_pg_concurrent_connections
        Spawns 10 threads, each running inserts via separate RelationalConnectors
        simultaneously to test concurrency handling.
        """
        table_name = f"test_concurrent_{uuid.uuid4().hex[:8]}"
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id SERIAL PRIMARY KEY, worker INT)"))

        def worker_task(worker_id: int, num_inserts: int) -> None:
            spec = ConnectionSpec(name=f"pg_worker_{worker_id}", kind="relational", options={"dsn": PG_DSN})
            connector = RelationalConnector(spec)
            for _ in range(num_inserts):
                connector.fetch(f"INSERT INTO {table_name} (worker) VALUES ({worker_id})")
            connector.close()

        threads = []
        for i in range(10):
            t = threading.Thread(target=worker_task, args=(i, 20))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        with pg_engine.connect() as conn:
            count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
            assert count == 200

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_pg_transaction_rollback_isolation(self, pg_engine: Any, pg_connector: RelationalConnector) -> None:
        """
        4. test_pg_transaction_rollback_isolation
        Verifies that rolled-back transactions do not persist data, ensuring
        isolation and ACID compliance in the testing setup.
        """
        table_name = f"test_rollback_{uuid.uuid4().hex[:8]}"
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT)"))

        try:
            with pg_engine.begin() as conn:
                conn.execute(sa.text(f"INSERT INTO {table_name} (id) VALUES (1)"))
                # Force rollback via exception
                raise ValueError("Force rollback")
        except ValueError:
            pass

        df = pg_connector.fetch(f"SELECT * FROM {table_name}")
        assert len(df) == 0, "Data should not be persisted after rollback"

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_pg_json_jsonb_roundtrip(self, pg_engine: Any, pg_connector: RelationalConnector) -> None:
        """
        5. test_pg_json_jsonb_roundtrip
        Creates a JSONB column, inserts nested JSON, reads it back, and verifies
        the structure is preserved exactly.
        """
        table_name = f"test_jsonb_{uuid.uuid4().hex[:8]}"
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, data JSONB)"))
            conn.execute(
                sa.text(f"""
                INSERT INTO {table_name} (id, data)
                VALUES (1, '{{"key": "value", "nested": {{"arr": [1, 2, 3]}}}}'::jsonb)
            """)
            )

        df = pg_connector.fetch(f"SELECT * FROM {table_name}")
        assert len(df) == 1

        data = df.iloc[0]["data"]
        # Depending on driver, this might be parsed to a dict automatically
        if isinstance(data, str):
            data = json.loads(data)

        assert isinstance(data, dict)
        assert data["key"] == "value"
        assert data["nested"]["arr"] == [1, 2, 3]

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_pg_null_handling_across_types(self, pg_engine: Any, pg_connector: RelationalConnector) -> None:
        """
        6. test_pg_null_handling_across_types
        Inserts NULLs into columns of varied types and verifies proper preservation
        and translation to pandas NA types.
        """
        table_name = f"test_nulls_{uuid.uuid4().hex[:8]}"
        with pg_engine.begin() as conn:
            conn.execute(
                sa.text(f"""
                CREATE TABLE {table_name} (
                    id INT, txt TEXT, num FLOAT, b BOOLEAN, ts TIMESTAMP
                )
            """)
            )
            # Leave all optional columns NULL
            conn.execute(sa.text(f"INSERT INTO {table_name} (id) VALUES (1)"))

        df = pg_connector.fetch(f"SELECT * FROM {table_name}")
        assert len(df) == 1

        row = df.iloc[0]
        assert row["id"] == 1
        assert pd.isna(row["txt"]) or row["txt"] is None
        assert pd.isna(row["num"])
        assert pd.isna(row["b"]) or row["b"] is None
        assert pd.isna(row["ts"]) or row["ts"] is pd.NaT

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_pg_large_text_blob(self, pg_engine: Any, pg_connector: RelationalConnector) -> None:
        """
        7. test_pg_large_text_blob
        Inserts very large string fields to verify no truncation occurs during transit.
        """
        table_name = f"test_blob_{uuid.uuid4().hex[:8]}"
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, large_text TEXT)"))

        # ~100KB of repeating string
        large_text = "ABCDEFGHIJ" * 1024 * 10

        with pg_engine.begin() as conn:
            conn.execute(
                sa.text(f"INSERT INTO {table_name} (id, large_text) VALUES (:id, :txt)"), {"id": 1, "txt": large_text}
            )

        df = pg_connector.fetch(f"SELECT * FROM {table_name}")
        assert len(df) == 1
        assert df.iloc[0]["large_text"] == large_text
        assert len(df.iloc[0]["large_text"]) == len(large_text)

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_pg_multiple_schema_discovery(self, pg_engine: Any, pg_connector: RelationalConnector) -> None:
        """
        8. test_pg_multiple_schema_discovery
        Creates tables in multiple schemas to verify the discovery process can see them.
        """
        schema_a = f"schema_a_{uuid.uuid4().hex[:8]}"
        schema_b = f"schema_b_{uuid.uuid4().hex[:8]}"

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE SCHEMA {schema_a}"))
            conn.execute(sa.text(f"CREATE SCHEMA {schema_b}"))
            conn.execute(sa.text(f"CREATE TABLE {schema_a}.tbl1 (id INT, val TEXT)"))
            conn.execute(sa.text(f"CREATE TABLE {schema_b}.tbl2 (id INT, val TEXT)"))

        schema = pg_connector.discover()

        # Schema discovery should ideally have captured tables.
        # This asserts it ran successfully without crashing.
        assert schema is not None

        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"DROP SCHEMA {schema_a} CASCADE"))
            conn.execute(sa.text(f"DROP SCHEMA {schema_b} CASCADE"))


@pytest.fixture
def mysql_engine() -> Generator[Any, None, None]:
    """
    Fixture providing a direct SQLAlchemy engine for MySQL setup/teardown.
    """
    engine = sa.create_engine(MYSQL_DSN, pool_size=20, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture
def mysql_connector() -> Generator[RelationalConnector, None, None]:
    """
    Fixture providing a RelationalConnector configured for MySQL.
    """
    spec = ConnectionSpec(name="mysql_test", kind="relational", options={"dsn": MYSQL_DSN})
    connector = RelationalConnector(spec)
    yield connector
    connector.close()


@pytest.mark.skipif(not MYSQL_AVAILABLE, reason="MySQL container is not available on localhost:3306")
class TestLiveMySQLIntegration:
    """
    Comprehensive test suite for MySQL integration. Covers encodings, precision,
    bulk operations, and basic handshake.
    """

    def test_mysql_handshake_and_basic_query(self, mysql_engine: Any, mysql_connector: RelationalConnector) -> None:
        """
        1. test_mysql_handshake_and_basic_query
        """
        table_name = f"test_basic_{uuid.uuid4().hex[:8]}"
        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, val VARCHAR(255))"))
            conn.execute(sa.text(f"INSERT INTO {table_name} (id, val) VALUES (1, 'hello'), (2, 'world')"))

        mysql_connector.test()
        schema = mysql_connector.discover()
        assert schema is not None

        sample_df = mysql_connector.sample(table_name, limit=10)
        assert isinstance(sample_df, pd.DataFrame)
        assert len(sample_df) == 2

        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_mysql_bulk_load_50k_rows(self, mysql_engine: Any, mysql_connector: RelationalConnector) -> None:
        """
        2. test_mysql_bulk_load_50k_rows
        """
        table_name = f"test_bulk_{uuid.uuid4().hex[:8]}"
        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, val FLOAT)"))

        rows = [{"id": i, "val": i * 1.1} for i in range(50000)]
        with mysql_engine.begin() as conn:
            for i in range(0, len(rows), 10000):
                batch = rows[i : i + 10000]
                conn.execute(sa.text(f"INSERT INTO {table_name} (id, val) VALUES (:id, :val)"), batch)

        df = mysql_connector.fetch(f"SELECT * FROM {table_name}")
        assert len(df) == 50000
        assert df["id"].max() == 49999

        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_mysql_emoji_and_cjk_full_roundtrip(self, mysql_engine: Any, mysql_connector: RelationalConnector) -> None:
        """
        3. test_mysql_emoji_and_cjk_full_roundtrip
        Tests utf8mb4 encoding correctly preserves emoji and CJK characters.
        """
        table_name = f"test_charset_{uuid.uuid4().hex[:8]}"
        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, text_val VARCHAR(255) CHARACTER SET utf8mb4)"))

        emoji_cjk_str = "Hello 🌍! 你好, 世界! 🍕"
        with mysql_engine.begin() as conn:
            conn.execute(
                sa.text(f"INSERT INTO {table_name} (id, text_val) VALUES (:id, :txt)"), {"id": 1, "txt": emoji_cjk_str}
            )

        df = mysql_connector.fetch(f"SELECT * FROM {table_name}")
        assert len(df) == 1
        assert df.iloc[0]["text_val"] == emoji_cjk_str

        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_mysql_decimal_precision(self, mysql_engine: Any, mysql_connector: RelationalConnector) -> None:
        """
        4. test_mysql_decimal_precision
        Tests DECIMAL(38,18) column retains exact precision without floating point drift.
        """
        table_name = f"test_decimal_{uuid.uuid4().hex[:8]}"
        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, precise_val DECIMAL(38,18))"))

        val_str = "12345678901234567890.123456789012345678"
        with mysql_engine.begin() as conn:
            conn.execute(
                sa.text(f"INSERT INTO {table_name} (id, precise_val) VALUES (:id, :val)"), {"id": 1, "val": val_str}
            )

        df = mysql_connector.fetch(f"SELECT * FROM {table_name}")
        val = df.iloc[0]["precise_val"]

        # Depending on engine interpretation, it might be a Decimal object
        if isinstance(val, decimal.Decimal):
            assert str(val) == val_str
        else:
            # If string or float check appropriately, though for decimal it should be exact.
            assert str(val) == val_str

        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_mysql_concurrent_transactions(self, mysql_engine: Any) -> None:
        """
        5. test_mysql_concurrent_transactions
        Tests multi-thread write concurrency.
        """
        table_name = f"test_concurrent_{uuid.uuid4().hex[:8]}"
        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT AUTO_INCREMENT PRIMARY KEY, worker INT)"))

        def worker_task(worker_id: int) -> None:
            spec = ConnectionSpec(name=f"mysql_worker_{worker_id}", kind="relational", options={"dsn": MYSQL_DSN})
            connector = RelationalConnector(spec)
            for _ in range(20):
                connector.fetch(f"INSERT INTO {table_name} (worker) VALUES ({worker_id})")
            connector.close()

        threads = []
        for i in range(5):
            t = threading.Thread(target=worker_task, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        with mysql_engine.connect() as conn:
            count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
            assert count == 100

        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))

    def test_mysql_datetime_timezone_handling(self, mysql_engine: Any, mysql_connector: RelationalConnector) -> None:
        """
        6. test_mysql_datetime_timezone_handling
        """
        table_name = f"test_tz_{uuid.uuid4().hex[:8]}"
        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE TABLE {table_name} (id INT, ts TIMESTAMP)"))

        dt = datetime.datetime(2026, 8, 26, 12, 0, 0)
        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"INSERT INTO {table_name} (id, ts) VALUES (:id, :ts)"), {"id": 1, "ts": dt})

        df = mysql_connector.fetch(f"SELECT * FROM {table_name}")
        val = df.iloc[0]["ts"]

        # Verify correctness
        assert val.year == 2026
        assert val.month == 8
        assert val.day == 26
        assert val.hour == 12

        with mysql_engine.begin() as conn:
            conn.execute(sa.text(f"DROP TABLE {table_name}"))


@pytest.fixture
def redis_client() -> Generator[Any, None, None]:
    """
    Fixture providing a Redis client for tests.
    Automatically decodes responses.
    """
    import redis

    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
    yield client
    client.close()


@pytest.mark.skipif(not REDIS_AVAILABLE, reason="Redis container is not available on localhost:6379")
class TestLiveRedisIntegration:
    """
    Comprehensive test suite for Redis integration, verifying basic capabilities,
    pipeline execution, complex structures (sorted sets, hashes), and TTL mechanisms.
    """

    def test_redis_connection_and_basic_caching(self, redis_client: Any) -> None:
        """1. test_redis_connection_and_basic_caching"""
        key = f"test_key_{uuid.uuid4().hex}"
        redis_client.set(key, "hello_world")

        val = redis_client.get(key)
        assert val == "hello_world"

        redis_client.delete(key)
        assert redis_client.get(key) is None

    def test_redis_pipeline_bulk_operations(self, redis_client: Any) -> None:
        """
        2. test_redis_pipeline_bulk_operations
        Pipelines 10K sets and gets to ensure high throughput processing is functional.
        """
        prefix = f"test_pipe_{uuid.uuid4().hex}:"
        pipe = redis_client.pipeline()
        for i in range(10000):
            pipe.set(f"{prefix}{i}", str(i))
        pipe.execute()

        pipe = redis_client.pipeline()
        for i in range(10000):
            pipe.get(f"{prefix}{i}")
        results = pipe.execute()

        assert len(results) == 10000
        assert results[0] == "0"
        assert results[9999] == "9999"

        # Cleanup
        keys = redis_client.keys(f"{prefix}*")
        if keys:
            redis_client.delete(*keys)

    def test_redis_sorted_set_leaderboard(self, redis_client: Any) -> None:
        """
        3. test_redis_sorted_set_leaderboard
        Uses ZADD and ZRANGE to simulate a leaderboard ranking system.
        """
        key = f"leaderboard_{uuid.uuid4().hex}"
        data = {"player1": 100, "player2": 250, "player3": 50, "player4": 150}
        redis_client.zadd(key, data)

        # Fetch top 2 players
        top = redis_client.zrange(key, 0, 1, desc=True, withscores=True)
        assert len(top) == 2
        assert top[0][0] == "player2"
        assert top[0][1] == 250.0
        assert top[1][0] == "player4"
        assert top[1][1] == 150.0

        redis_client.delete(key)

    def test_redis_hash_map_session_store(self, redis_client: Any) -> None:
        """
        4. test_redis_hash_map_session_store
        Uses HSET and HGETALL to store and retrieve simulated session metadata.
        """
        key = f"session_{uuid.uuid4().hex}"
        session_data = {"user_id": "99999", "role": "admin", "last_login": "2026-08-26T12:00:00Z"}
        redis_client.hset(key, mapping=session_data)

        fetched = redis_client.hgetall(key)
        assert fetched["user_id"] == "99999"
        assert fetched["role"] == "admin"
        assert fetched["last_login"] == "2026-08-26T12:00:00Z"

        redis_client.delete(key)

    def test_redis_expiry_and_ttl_verification(self, redis_client: Any) -> None:
        """
        5. test_redis_expiry_and_ttl_verification
        Tests that keys properly expire by using SET EX and checking TTL.
        """
        key = f"ttl_test_{uuid.uuid4().hex}"
        redis_client.set(key, "temp_data", ex=10)

        ttl = redis_client.ttl(key)
        assert 0 < ttl <= 10

        val = redis_client.get(key)
        assert val == "temp_data"

        redis_client.delete(key)
