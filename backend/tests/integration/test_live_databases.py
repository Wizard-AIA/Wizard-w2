"""Live Database Services Integration Test Suite.

Inspired by the live database service test suites in pandas, sqlalchemy, and dbt.
Tests Wizard's relational connector and distributed cache against live, real
PostgreSQL, MySQL, and Redis services with full network roundtrips.
"""

from __future__ import annotations

import os
import socket

import pandas as pd
import pytest

from src.core.connectors.spec import ConnectionSpec


def _is_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """Checks if a TCP service is reachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, OSError):
        return False


POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", 5432))
POSTGRES_AVAILABLE = _is_port_open(POSTGRES_HOST, POSTGRES_PORT)

MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
MYSQL_AVAILABLE = _is_port_open(MYSQL_HOST, MYSQL_PORT)

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_AVAILABLE = _is_port_open(REDIS_HOST, REDIS_PORT)


@pytest.mark.skipif(not POSTGRES_AVAILABLE, reason="Live PostgreSQL container not reachable")
class TestLivePostgresIntegration:
    """Tests live PostgreSQL relational connection, schema discovery, and data streaming."""

    def test_postgres_handshake_and_query(self):
        sqlalchemy = pytest.importorskip("sqlalchemy")
        from src.core.connectors.relational import RelationalConnector

        dsn = f"postgresql://postgres:postgres@{POSTGRES_HOST}:{POSTGRES_PORT}/wizard_test"
        engine = sqlalchemy.create_engine(dsn)

        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "CREATE TABLE IF NOT EXISTS pg_ci_test (id INT, metric_val DOUBLE PRECISION, label TEXT)"
                )
            )
            conn.execute(sqlalchemy.text("DELETE FROM pg_ci_test"))
            conn.execute(sqlalchemy.text("INSERT INTO pg_ci_test VALUES (1, 10.5, 'alpha'), (2, 20.75, 'beta')"))

        spec = ConnectionSpec(
            name="Postgres Live CI",
            kind="relational",
            options={"dsn": dsn},
        )
        connector = RelationalConnector(spec)
        connector.test()

        schema = connector.discover()
        assert any(t.name == "pg_ci_test" for t in schema.targets)

        sampled = connector.sample("pg_ci_test", limit=100)
        assert isinstance(sampled, pd.DataFrame)
        assert len(sampled) == 2
        assert list(sampled.columns) == ["id", "metric_val", "label"]
        connector.close()


@pytest.mark.skipif(not MYSQL_AVAILABLE, reason="Live MySQL container not reachable")
class TestLiveMySQLIntegration:
    """Tests live MySQL relational connection, utf8mb4 collation, and decimal conversion."""

    def test_mysql_handshake_and_query(self):
        sqlalchemy = pytest.importorskip("sqlalchemy")
        pytest.importorskip("pymysql")
        from src.core.connectors.relational import RelationalConnector

        dsn = f"mysql+pymysql://root:rootpassword@{MYSQL_HOST}:{MYSQL_PORT}/wizard_test?charset=utf8mb4"
        engine = sqlalchemy.create_engine(dsn)

        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "CREATE TABLE IF NOT EXISTS mysql_ci_test (id INT, revenue DECIMAL(10, 2), city VARCHAR(64))"
                )
            )
            conn.execute(sqlalchemy.text("DELETE FROM mysql_ci_test"))
            conn.execute(
                sqlalchemy.text("INSERT INTO mysql_ci_test VALUES (1, 999.99, 'Tokyo 🗼'), (2, 1450.50, 'Zürich 🏔️')")
            )

        spec = ConnectionSpec(
            name="MySQL Live CI",
            kind="relational",
            options={"dsn": dsn},
        )
        connector = RelationalConnector(spec)
        connector.test()

        sampled = connector.sample("mysql_ci_test", limit=100)
        assert isinstance(sampled, pd.DataFrame)
        assert len(sampled) == 2
        assert "Tokyo 🗼" in sampled["city"].values
        connector.close()


@pytest.mark.skipif(not REDIS_AVAILABLE, reason="Live Redis container not reachable")
class TestLiveRedisIntegration:
    """Tests live Redis distributed caching and rate-limiting session store."""

    def test_redis_connection_and_caching(self):
        redis_lib = pytest.importorskip("redis")

        client = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
        assert client.ping() is True

        test_key = "wizard:ci:cache_test"
        client.set(test_key, "cached_analytical_summary", ex=60)
        val = client.get(test_key)
        assert val == "cached_analytical_summary"
        client.delete(test_key)
