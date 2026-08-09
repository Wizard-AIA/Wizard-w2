import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np

from src.config import settings
from src.utils.logging import logger


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS semantic_cache (
        query TEXT PRIMARY KEY,
        schema_hash TEXT,
        columns TEXT,
        code TEXT,
        embedding BLOB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trajectories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instruction TEXT,
        schema_hash TEXT,
        columns TEXT,
        failed_code TEXT,
        error_message TEXT,
        corrected_code TEXT,
        embedding BLOB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedbacks (
        task TEXT PRIMARY KEY,
        code TEXT,
        embedding BLOB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS working_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        session_id TEXT,
        instruction TEXT,
        plan TEXT,
        code TEXT,
        result TEXT,
        meta TEXT,
        embedding BLOB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_registry (
        filename TEXT PRIMARY KEY,
        session_id TEXT,
        columns TEXT,
        row_count INTEGER,
        primary_key TEXT,
        meta TEXT
    )
    """,
    # Recurring analyses, offered to the user for promotion into a named skill.
    #
    # There is deliberately no `session_id`. "You keep doing this" is a claim
    # about many sessions, so a candidate must outlive the one that last bumped
    # it -- which is also why `delete_session_data` does not touch this table and
    # why the test teardown has to clear it explicitly.
    """
    CREATE TABLE IF NOT EXISTS skill_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        instruction TEXT,
        columns TEXT,
        occurrences INTEGER DEFAULT 1,
        first_seen REAL,
        last_seen REAL,
        plan TEXT,
        code TEXT,
        promoted_to TEXT,
        dismissed INTEGER DEFAULT 0,
        embedding BLOB
    )
    """,
    # Which analyses used which skill -- the half of the milestone's skills
    # browser that the `skill` frame alone cannot answer, since that frame is
    # live and a browser is opened later.
    #
    # No `session_id`, for the same reason `skill_candidates` has none: "this
    # skill has informed eleven analyses" is a claim about the install, not about
    # one browser tab, and a TTL reap would otherwise reset a skill's history to
    # nothing while the skill itself remained.
    """
    CREATE TABLE IF NOT EXISTS skill_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        skill TEXT NOT NULL,
        instruction TEXT,
        timestamp REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        timestamp REAL NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        meta TEXT
    )
    """,
)

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_semantic_cache_schema ON semantic_cache(schema_hash)",
    "CREATE INDEX IF NOT EXISTS idx_trajectories_schema ON trajectories(schema_hash)",
    "CREATE INDEX IF NOT EXISTS idx_working_memory_session ON working_memory(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_working_memory_ts ON working_memory(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_schema_registry_session ON schema_registry(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_skill_candidates_kind ON skill_candidates(kind, dismissed)",
    "CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage(skill, timestamp)",
)

# Columns added after the initial release, applied idempotently on boot.
MIGRATIONS = (
    ("semantic_cache", "schema_hash", "TEXT"),
    ("trajectories", "schema_hash", "TEXT"),
    ("working_memory", "session_id", "TEXT"),
    ("working_memory", "embedding", "BLOB"),
    ("schema_registry", "session_id", "TEXT"),
)


class DatabaseManager:
    """
    Unified SQLite store for the semantic cache, failure trajectories, feedback
    examples, working memory, chat transcripts and the multi-file schema registry.

    Connections are pooled per thread and closed deterministically. WAL journalling
    plus a busy timeout are required because FastAPI dispatches blocking work through
    ``asyncio.to_thread``, so several threads hit this database concurrently.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = str(db_path) if db_path else str(settings.DATA_DIR / "wizard.db")
        # Ensure parent directory exists
        from pathlib import Path

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def _connection(self) -> sqlite3.Connection:
        """Returns a per-thread connection, creating it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # `isolation_level=None` puts the connection in autocommit mode so
            # transaction boundaries are exactly what `_write` states below --
            # no implicit, *deferred* BEGIN on the first DML statement that
            # would only take SQLite's write lock partway through the block.
            conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA foreign_keys=ON")
            except sqlite3.Error as exc:  # pragma: no cover - pragma support varies
                logger.warning("Failed to apply SQLite pragmas", error=str(exc))
            self._local.conn = conn
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        yield self._connection()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction. SQLite allows a single writer at a time.

        ``BEGIN IMMEDIATE`` takes SQLite's write lock as soon as the
        transaction opens, instead of the default *deferred* BEGIN that only
        acquires it on the first write statement. Under WAL, two connections
        that both start deferred and then try to upgrade to a writer can each
        end up waiting on the other's read lock -- the classic SQLite
        "database is locked" deadlock. Acquiring the lock immediate, while
        still serialised through ``_write_lock`` for connections that share
        this process, closes that window rather than trusting timing alone.
        """
        conn = self._connection()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self):
        """Closes the calling thread's connection (used by tests and shutdown)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as exc:
                logger.debug("Connection close failed during shutdown", error=str(exc))
            self._local.conn = None

    def _init_db(self):
        """Creates tables and indexes, then applies additive column migrations."""
        try:
            with self._write() as conn:
                for statement in SCHEMA_STATEMENTS:
                    conn.execute(statement)

                for table, column, coltype in MIGRATIONS:
                    cursor = conn.execute(f"PRAGMA table_info({table})")
                    existing = {row["name"] for row in cursor.fetchall()}
                    if column not in existing:
                        logger.info("Migrating database", table=table, column=column)
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

                for statement in INDEX_STATEMENTS:
                    conn.execute(statement)
            logger.info("SQLite database initialized", path=self.db_path)
        except Exception as e:
            logger.error("Failed to initialize SQLite database", error=str(e))

    # ------------------------------------------------------------------ #
    # Vector serialization
    # ------------------------------------------------------------------ #
    @staticmethod
    def _serialize_vector(vec: np.ndarray) -> bytes:
        return np.asarray(vec, dtype=np.float32).tobytes()

    @staticmethod
    def _deserialize_vector(blob: bytes | None) -> np.ndarray | None:
        if not blob:
            return None
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def _schema_hash(columns: list[str]) -> str:
        return ",".join(sorted(columns))

    # ------------------------------------------------------------------ #
    # Semantic Cache
    # ------------------------------------------------------------------ #
    def get_cache_entries(self, active_columns: list[str] | None = None) -> list[dict[str, Any]]:
        try:
            with self._read() as conn:
                if active_columns is not None:
                    rows = conn.execute(
                        "SELECT query, columns, code, embedding FROM semantic_cache WHERE schema_hash = ?",
                        (self._schema_hash(active_columns),),
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT query, columns, code, embedding FROM semantic_cache").fetchall()

                return [
                    {
                        "query": row["query"],
                        "columns": json.loads(row["columns"]),
                        "code": row["code"],
                        "embedding": self._deserialize_vector(row["embedding"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Failed to fetch semantic cache from database", error=str(e))
            return []

    def save_cache_entry(self, query: str, columns: list[str], code: str, embedding: np.ndarray):
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO semantic_cache (query, schema_hash, columns, code, embedding)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        query.strip().lower(),
                        self._schema_hash(columns),
                        json.dumps(columns),
                        code,
                        self._serialize_vector(embedding),
                    ),
                )
        except Exception as e:
            logger.error("Failed to save semantic cache entry", error=str(e))

    def clear_cache(self):
        try:
            with self._write() as conn:
                conn.execute("DELETE FROM semantic_cache")
        except Exception as e:
            logger.error("Failed to clear semantic cache", error=str(e))

    # ------------------------------------------------------------------ #
    # Trajectories (failure -> fix memory)
    # ------------------------------------------------------------------ #
    def get_trajectory_entries(self, active_columns: list[str] | None = None) -> list[dict[str, Any]]:
        try:
            with self._read() as conn:
                base = "SELECT instruction, columns, failed_code, error_message, corrected_code, embedding FROM trajectories"
                if active_columns is not None:
                    rows = conn.execute(
                        f"{base} WHERE schema_hash = ?", (self._schema_hash(active_columns),)
                    ).fetchall()
                else:
                    rows = conn.execute(base).fetchall()

                return [
                    {
                        "instruction": row["instruction"],
                        "columns": json.loads(row["columns"]),
                        "failed_code": row["failed_code"],
                        "error_message": row["error_message"],
                        "corrected_code": row["corrected_code"],
                        "embedding": self._deserialize_vector(row["embedding"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Failed to fetch trajectories from database", error=str(e))
            return []

    def save_trajectory(
        self,
        instruction: str,
        columns: list[str],
        failed_code: str,
        error_message: str,
        corrected_code: str,
        embedding: np.ndarray | None,
    ):
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO trajectories"
                    " (instruction, schema_hash, columns, failed_code, error_message, corrected_code, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        instruction.strip().lower(),
                        self._schema_hash(columns),
                        json.dumps(columns),
                        failed_code,
                        error_message,
                        corrected_code,
                        self._serialize_vector(embedding) if embedding is not None else None,
                    ),
                )
        except Exception as e:
            logger.error("Failed to save trajectory memory", error=str(e))

    # ------------------------------------------------------------------ #
    # Skill candidates (recurring analyses, offered for promotion)
    # ------------------------------------------------------------------ #
    def get_skill_candidates(self, kind: str | None = None, include_settled: bool = False) -> list[dict[str, Any]]:
        """Candidates, newest activity first.

        ``include_settled`` brings back the ones already promoted or dismissed,
        which only the clustering path wants: a dismissed candidate must still be
        *matched* against, or the next occurrence inserts a fresh row and the
        offer the user just declined comes straight back.
        """
        try:
            with self._read() as conn:
                clauses: list[str] = []
                params: list[Any] = []
                if kind:
                    clauses.append("kind = ?")
                    params.append(kind)
                if not include_settled:
                    clauses.append("dismissed = 0 AND promoted_to IS NULL")
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = conn.execute(
                    "SELECT id, kind, instruction, columns, occurrences, first_seen, last_seen,"
                    f" plan, code, promoted_to, dismissed, embedding FROM skill_candidates{where}"
                    " ORDER BY last_seen DESC",
                    tuple(params),
                ).fetchall()

                return [
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "instruction": row["instruction"],
                        "columns": json.loads(row["columns"] or "[]"),
                        "occurrences": row["occurrences"],
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                        "plan": row["plan"] or "",
                        "code": row["code"] or "",
                        "promoted_to": row["promoted_to"],
                        "dismissed": bool(row["dismissed"]),
                        "embedding": self._deserialize_vector(row["embedding"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Failed to fetch skill candidates", error=str(e))
            return []

    def add_skill_candidate(
        self,
        kind: str,
        instruction: str,
        columns: list[str],
        plan: str,
        code: str,
        embedding: np.ndarray | None,
    ) -> int:
        """Records a first occurrence. Returns the new row id, or 0 on failure."""
        now = time.time()
        try:
            with self._write() as conn:
                cursor = conn.execute(
                    "INSERT INTO skill_candidates"
                    " (kind, instruction, columns, occurrences, first_seen, last_seen, plan, code, embedding)"
                    " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (
                        kind,
                        instruction.strip(),
                        json.dumps(columns),
                        now,
                        now,
                        plan,
                        code,
                        self._serialize_vector(embedding) if embedding is not None else None,
                    ),
                )
                return int(cursor.lastrowid or 0)
        except Exception as e:
            logger.error("Failed to record a skill candidate", error=str(e))
            return 0

    def bump_skill_candidate(self, candidate_id: int, plan: str = "", code: str = "") -> int:
        """Counts another occurrence. Returns the new count, or 0 on failure.

        The plan and code are refreshed rather than appended: what the user would
        promote is how they do this *now*, and the first attempt at a recurring
        analysis is usually the worst one.
        """
        try:
            with self._write() as conn:
                conn.execute(
                    "UPDATE skill_candidates SET occurrences = occurrences + 1, last_seen = ?,"
                    " plan = COALESCE(NULLIF(?, ''), plan), code = COALESCE(NULLIF(?, ''), code)"
                    " WHERE id = ?",
                    (time.time(), plan, code, candidate_id),
                )
                row = conn.execute("SELECT occurrences FROM skill_candidates WHERE id = ?", (candidate_id,)).fetchone()
                return int(row["occurrences"]) if row else 0
        except Exception as e:
            logger.error("Failed to bump a skill candidate", error=str(e))
            return 0

    def settle_skill_candidate(self, candidate_id: int, promoted_to: str | None = None) -> bool:
        """Marks a candidate promoted (with the skill's name) or dismissed."""
        try:
            with self._write() as conn:
                if promoted_to:
                    cursor = conn.execute(
                        "UPDATE skill_candidates SET promoted_to = ? WHERE id = ?", (promoted_to, candidate_id)
                    )
                else:
                    cursor = conn.execute("UPDATE skill_candidates SET dismissed = 1 WHERE id = ?", (candidate_id,))
                # Zero rows matched means the id is unknown. Returning True there
                # made the dismiss route answer 200 for a candidate that never
                # existed -- `promotion.dismiss` hands this straight to the 404.
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("Failed to settle a skill candidate", error=str(e))
            return False

    def clear_skill_candidates(self):
        """Removes every candidate. For the test suite's teardown.

        These rows outlive a session on purpose, which without this means they
        outlive a *test* -- and an occurrence count carried into the next test
        makes a promotion threshold fire in a test that never asked a question
        twice. Order-dependent and invisible when the file is run alone.
        """
        try:
            with self._write() as conn:
                conn.execute("DELETE FROM skill_candidates")
        except Exception as e:
            logger.error("Failed to clear skill candidates", error=str(e))

    # ------------------------------------------------------------------ #
    # Skill usage ("which analyses used which skill")
    # ------------------------------------------------------------------ #
    def record_skill_usage(self, skills: list[str], instruction: str):
        """Notes that these skills informed this question.

        Written once per turn rather than once per retrieval: a skill can match
        at planning and again through ``consult``, and the browser's claim is
        "this skill informed that analysis", not "it was read twice".
        """
        rows = [(name, (instruction or "").strip()[:500], time.time()) for name in skills if name]
        if not rows:
            return
        try:
            with self._write() as conn:
                conn.executemany("INSERT INTO skill_usage (skill, instruction, timestamp) VALUES (?, ?, ?)", rows)
        except Exception as e:
            logger.error("Failed to record skill usage", error=str(e))

    def skill_usage_summary(self) -> dict[str, dict]:
        """Per skill: how many analyses it informed, and when it last did.

        One aggregate query for every skill rather than one per skill, because
        this renders on a page that lists all of them.
        """
        try:
            with self._read() as conn:
                rows = conn.execute(
                    "SELECT skill, COUNT(*) AS uses, MAX(timestamp) AS last_used FROM skill_usage GROUP BY skill"
                ).fetchall()
            return {row["skill"]: {"uses": int(row["uses"]), "last_used": row["last_used"]} for row in rows}
        except Exception as e:
            logger.error("Failed to read skill usage", error=str(e))
            return {}

    def get_skill_usage(self, skill: str, limit: int = 10) -> list[dict]:
        """The most recent questions this skill informed, newest first."""
        try:
            with self._read() as conn:
                rows = conn.execute(
                    "SELECT instruction, timestamp FROM skill_usage WHERE skill = ? ORDER BY timestamp DESC LIMIT ?",
                    (skill, limit),
                ).fetchall()
            return [{"instruction": row["instruction"] or "", "timestamp": row["timestamp"]} for row in rows]
        except Exception as e:
            logger.error("Failed to read skill usage", error=str(e))
            return []

    def clear_skill_usage(self):
        """Removes every usage row. For the test suite's teardown.

        Same reasoning as ``clear_skill_candidates``: no ``session_id`` means
        nothing else clears these, and a count carried into the next test makes a
        freshly written skill look like one that has been used for months.
        """
        try:
            with self._write() as conn:
                conn.execute("DELETE FROM skill_usage")
        except Exception as e:
            logger.error("Failed to clear skill usage", error=str(e))

    # ------------------------------------------------------------------ #
    # Feedbacks (few-shot successes)
    # ------------------------------------------------------------------ #
    def get_feedbacks(self) -> list[dict[str, Any]]:
        try:
            with self._read() as conn:
                rows = conn.execute("SELECT task, code, embedding FROM feedbacks").fetchall()
                return [
                    {
                        "task": row["task"],
                        "code": row["code"],
                        "embedding": self._deserialize_vector(row["embedding"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Failed to fetch feedbacks from database", error=str(e))
            return []

    def save_feedback(self, task: str, code: str, embedding: np.ndarray | None = None):
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO feedbacks (task, code, embedding) VALUES (?, ?, ?)",
                    (
                        task.strip().lower(),
                        code,
                        self._serialize_vector(embedding) if embedding is not None else None,
                    ),
                )
        except Exception as e:
            logger.error("Failed to save feedback entry", error=str(e))

    # ------------------------------------------------------------------ #
    # Working Memory
    # ------------------------------------------------------------------ #
    def get_memories(self, session_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        try:
            with self._read() as conn:
                sql = (
                    "SELECT timestamp, session_id, instruction, plan, code, result, meta, embedding FROM working_memory"
                )
                params: list[Any] = []
                if session_id:
                    sql += " WHERE session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY timestamp ASC"
                if limit:
                    sql += " LIMIT ?"
                    params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [self._memory_row(row) for row in rows]
        except Exception as e:
            logger.error("Failed to fetch working memory from database", error=str(e))
            return []

    def get_recent_memories(self, session_id: str | None, timespan_seconds: int) -> list[dict[str, Any]]:
        """Memories newer than ``timespan_seconds``, oldest first."""
        import time

        cutoff = time.time() - timespan_seconds
        try:
            with self._read() as conn:
                sql = (
                    "SELECT timestamp, session_id, instruction, plan, code, result, meta, embedding"
                    " FROM working_memory WHERE timestamp >= ?"
                )
                params: list[Any] = [cutoff]
                if session_id:
                    sql += " AND session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY timestamp ASC"
                rows = conn.execute(sql, params).fetchall()
                return [self._memory_row(row) for row in rows]
        except Exception as e:
            logger.error("Failed to fetch recent working memory", error=str(e))
            return []

    def search_memories(self, query: str, limit: int = 3, session_id: str | None = None) -> list[dict[str, Any]]:
        """Keyword fallback search. Vector search lives in the RAG retriever."""
        try:
            query_terms = [f"%{term.strip().lower()}%" for term in query.split() if term.strip()]
            if not query_terms:
                return []

            where_clauses = []
            params: list[Any] = []
            for term in query_terms:
                where_clauses.append("(LOWER(instruction) LIKE ? OR LOWER(plan) LIKE ?)")
                params.extend([term, term])

            if session_id:
                where_clauses.append("session_id = ?")
                params.append(session_id)

            sql = (
                "SELECT timestamp, session_id, instruction, plan, code, result, meta, embedding FROM working_memory"
                f" WHERE {' AND '.join(where_clauses)} ORDER BY timestamp DESC LIMIT ?"
            )
            params.append(limit)

            with self._read() as conn:
                rows = conn.execute(sql, params).fetchall()
                return [self._memory_row(row) for row in rows]
        except Exception as e:
            logger.error("Failed to search working memory in database", error=str(e))
            return []

    def _memory_row(self, row: sqlite3.Row) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except (TypeError, ValueError) as exc:
            logger.debug("Could not parse a row's stored meta column", error=str(exc))
        keys = row.keys()
        return {
            "timestamp": row["timestamp"],
            "session_id": row["session_id"] if "session_id" in keys else None,
            "instruction": row["instruction"],
            "plan": row["plan"],
            "code": row["code"],
            "result": row["result"],
            "meta": meta,
            "embedding": self._deserialize_vector(row["embedding"]) if "embedding" in keys else None,
        }

    def save_memory(
        self,
        timestamp: float,
        instruction: str,
        plan: str,
        code: str,
        result: str,
        meta: dict[str, Any] | None = None,
        session_id: str | None = None,
        embedding: np.ndarray | None = None,
    ):
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO working_memory"
                    " (timestamp, session_id, instruction, plan, code, result, meta, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        timestamp,
                        session_id,
                        instruction,
                        plan,
                        code,
                        result,
                        json.dumps(meta or {}),
                        self._serialize_vector(embedding) if embedding is not None else None,
                    ),
                )
        except Exception as e:
            logger.error("Failed to save working memory entry", error=str(e))

    def prune_memories(self, keep_last: int = 500):
        """Bounds unbounded growth of the memory table."""
        try:
            with self._write() as conn:
                conn.execute(
                    "DELETE FROM working_memory WHERE id NOT IN"
                    " (SELECT id FROM working_memory ORDER BY timestamp DESC LIMIT ?)",
                    (keep_last,),
                )
        except Exception as e:
            logger.error("Failed to prune working memory", error=str(e))

    # ------------------------------------------------------------------ #
    # Chat transcripts (multi-turn context)
    # ------------------------------------------------------------------ #
    def append_chat_message(self, session_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> int:
        """Records one chat message. Returns the new row id, or 0 on failure.

        The id is what a later "export this turn" request keys on -- see
        `get_chat_message` -- since `meta` is the only place the ordered,
        actually-executed steps of a turn are persisted past the run itself.
        """
        import time

        try:
            with self._write() as conn:
                cursor = conn.execute(
                    "INSERT INTO chat_messages (session_id, timestamp, role, content, meta) VALUES (?, ?, ?, ?, ?)",
                    (session_id, time.time(), role, content, json.dumps(meta or {})),
                )
                return int(cursor.lastrowid or 0)
        except Exception as e:
            logger.error("Failed to append chat message", error=str(e))
            return 0

    def get_chat_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            with self._read() as conn:
                rows = conn.execute(
                    "SELECT role, content, timestamp, meta FROM chat_messages"
                    " WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
                messages = [
                    {"role": row["role"], "content": row["content"], "timestamp": row["timestamp"]} for row in rows
                ]
                messages.reverse()
                return messages
        except Exception as e:
            logger.error("Failed to fetch chat messages", error=str(e))
            return []

    def get_chat_message(self, session_id: str, message_id: int) -> dict[str, Any] | None:
        """One message by id, scoped to ``session_id`` so a message id from a
        different session can never be looked up -- the export route's only
        access check.
        """
        try:
            with self._read() as conn:
                row = conn.execute(
                    "SELECT id, role, content, timestamp, meta FROM chat_messages WHERE id = ? AND session_id = ?",
                    (message_id, session_id),
                ).fetchone()
                if row is None:
                    return None
                meta = json.loads(row["meta"]) if row["meta"] else {}
                return {
                    "id": row["id"],
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                    "meta": meta,
                }
        except Exception as e:
            logger.error("Failed to fetch chat message", error=str(e))
            return None

    def delete_session_data(self, session_id: str):
        try:
            with self._write() as conn:
                conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM working_memory WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM schema_registry WHERE session_id = ?", (session_id,))
        except Exception as e:
            logger.error("Failed to delete session data", error=str(e))

    # ------------------------------------------------------------------ #
    # Schema Registry
    # ------------------------------------------------------------------ #
    def save_schema(
        self,
        filename: str,
        columns: list[str],
        row_count: int,
        primary_key: str,
        meta: dict[str, Any] | None = None,
        session_id: str | None = None,
    ):
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_registry"
                    " (filename, session_id, columns, row_count, primary_key, meta) VALUES (?, ?, ?, ?, ?, ?)",
                    (filename, session_id, json.dumps(columns), row_count, primary_key, json.dumps(meta or {})),
                )
            logger.info("Saved schema to database registry", filename=filename)
        except Exception as e:
            logger.error("Failed to save schema registry entry", error=str(e))

    def get_schemas(self, session_id: str | None = None) -> list[dict[str, Any]]:
        try:
            with self._read() as conn:
                sql = "SELECT filename, session_id, columns, row_count, primary_key, meta FROM schema_registry"
                params: list[Any] = []
                if session_id:
                    sql += " WHERE session_id = ?"
                    params.append(session_id)
                rows = conn.execute(sql, params).fetchall()

                entries = []
                for row in rows:
                    try:
                        cols = json.loads(row["columns"]) if row["columns"] else []
                    except (TypeError, ValueError):
                        cols = []
                    try:
                        meta = json.loads(row["meta"]) if row["meta"] else {}
                    except (TypeError, ValueError):
                        meta = {}
                    entries.append(
                        {
                            "filename": row["filename"],
                            "session_id": row["session_id"],
                            "columns": cols,
                            "row_count": row["row_count"],
                            "primary_key": row["primary_key"],
                            "meta": meta,
                        }
                    )
                return entries
        except Exception as e:
            logger.error("Failed to fetch schemas from registry", error=str(e))
            return []

    def delete_schema(self, filename: str, session_id: str | None = None):
        try:
            with self._write() as conn:
                if session_id:
                    conn.execute(
                        "DELETE FROM schema_registry WHERE filename = ? AND session_id = ?", (filename, session_id)
                    )
                else:
                    conn.execute("DELETE FROM schema_registry WHERE filename = ?", (filename,))
            logger.info("Deleted schema from registry", filename=filename)
        except Exception as e:
            logger.error("Failed to delete schema registry entry", error=str(e))


# Singleton instance
db_mgr = DatabaseManager()
