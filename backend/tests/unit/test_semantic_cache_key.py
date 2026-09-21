"""A cached solution belongs to a question and to the data (and workflow) it was written for."""

from __future__ import annotations

import sqlite3

from src.core.database import SEMANTIC_CACHE_TABLE, DatabaseManager, db_mgr
from src.core.semantic_cache import semantic_cache


def _entries_for(query: str) -> list[dict]:
    return [entry for entry in db_mgr.get_cache_entries() if entry["query"] == query]


def test_the_same_question_in_two_scopes_keeps_both_solutions() -> None:
    """Keyed on the question alone, the second `add` replaced the first and the flows evicted each other."""
    columns = ["A", "B"]
    semantic_cache.add("total of a", columns, "print('direct')", scope="flow=direct")
    semantic_cache.add("total of a", columns, "print('agentic')", scope="flow=agentic")

    assert sorted(entry["code"] for entry in _entries_for("total of a")) == ["print('agentic')", "print('direct')"]


def test_the_same_question_on_two_datasets_keeps_both_solutions() -> None:
    semantic_cache.add("average price", ["price", "region"], "print('shop')")
    semantic_cache.add("average price", ["price", "sku"], "print('warehouse')")

    assert len(_entries_for("average price")) == 2


def test_storing_the_same_entry_again_replaces_it() -> None:
    semantic_cache.add("row count", ["A"], "print(1)")
    semantic_cache.add("row count", ["A"], "print(2)")

    entries = _entries_for("row count")
    assert [entry["code"] for entry in entries] == ["print(2)"]


def _old_database() -> sqlite3.Connection:
    """A cache table as v1.0.13 and earlier created it: `query` alone is the key."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE semantic_cache (query TEXT PRIMARY KEY, schema_hash TEXT, columns TEXT, code TEXT, embedding BLOB)"
    )
    conn.execute("INSERT INTO semantic_cache VALUES ('q1', 'h1', '[\"A\"]', 'code1', NULL)")
    conn.execute("INSERT INTO semantic_cache VALUES ('q2', NULL, '[\"B\"]', 'code2', NULL)")
    return conn


def _key(conn: sqlite3.Connection) -> list[str]:
    return sorted(row["name"] for row in conn.execute("PRAGMA table_info(semantic_cache)").fetchall() if row["pk"])


def test_an_existing_cache_is_moved_to_the_new_key_without_losing_entries() -> None:
    conn = _old_database()
    assert _key(conn) == ["query"]

    DatabaseManager._rekey_semantic_cache(conn)

    assert _key(conn) == ["query", "schema_hash"]
    rows = {row["query"]: row["code"] for row in conn.execute("SELECT query, code FROM semantic_cache")}
    assert rows == {"q1": "code1", "q2": "code2"}, "a NULL hash becomes the empty one, and nothing is dropped"
    # The point of the change: the same question under another hash is now a separate row.
    conn.execute("INSERT INTO semantic_cache VALUES ('q1', 'other', '[]', 'code3', NULL)")
    assert conn.execute("SELECT COUNT(*) FROM semantic_cache WHERE query = 'q1'").fetchone()[0] == 2


def test_migrating_twice_changes_nothing() -> None:
    conn = _old_database()
    DatabaseManager._rekey_semantic_cache(conn)
    before = [tuple(row) for row in conn.execute("SELECT * FROM semantic_cache ORDER BY query")]
    DatabaseManager._rekey_semantic_cache(conn)
    assert [tuple(row) for row in conn.execute("SELECT * FROM semantic_cache ORDER BY query")] == before


def test_opening_a_v1_0_13_database_upgrades_it_and_keeps_its_cache(tmp_path) -> None:
    """The real upgrade path: a file on disk with the old table, opened by the real constructor."""
    path = tmp_path / "wizard.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE semantic_cache (query TEXT PRIMARY KEY, schema_hash TEXT, columns TEXT, code TEXT, embedding BLOB)"
    )
    old.execute("CREATE INDEX idx_semantic_cache_schema ON semantic_cache(schema_hash)")
    old.execute("INSERT INTO semantic_cache VALUES ('kept', 'h', '[\"A\"]', 'print(1)', NULL)")
    old.commit()
    old.close()

    upgraded = DatabaseManager(str(path))

    with upgraded._read() as conn:
        key = sorted(row["name"] for row in conn.execute("PRAGMA table_info(semantic_cache)") if row["pk"])
        assert key == ["query", "schema_hash"]
        assert conn.execute("SELECT code FROM semantic_cache WHERE query = 'kept'").fetchone()[0] == "print(1)"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'idx_semantic_cache_schema'").fetchone()
    upgraded.close()


def test_a_fresh_database_is_created_with_the_new_key() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(SEMANTIC_CACHE_TABLE)
    assert _key(conn) == ["query", "schema_hash"]
    DatabaseManager._rekey_semantic_cache(conn)  # already right: no rebuild
    assert _key(conn) == ["query", "schema_hash"]
