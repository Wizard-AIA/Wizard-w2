import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
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
    # One row per turn's `AnalyticalState` snapshot (see core/analysis/state.py).
    # `message_id` links back to the `chat_messages` row for that turn's answer.
    """
    CREATE TABLE IF NOT EXISTS analysis_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id INTEGER,
        timestamp REAL NOT NULL,
        state TEXT NOT NULL
    )
    """,
    # One row per revision of a turn's plan (see core/analysis/plan.py). Also
    # embedded inside analysis_state's JSON blob; this table exists so history
    # can be queried directly without parsing that blob -- ADR 0002.
    """
    CREATE TABLE IF NOT EXISTS plan_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id INTEGER,
        revision_index INTEGER NOT NULL,
        text TEXT NOT NULL,
        why TEXT,
        timestamp REAL NOT NULL
    )
    """,
    # One row per node/edge of a turn's `EvidenceGraph` (see core/analysis/provenance.py).
    # Also embedded inside analysis_state's JSON blob; queryable directly for tracing.
    """
    CREATE TABLE IF NOT EXISTS evidence_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id INTEGER,
        node_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        data TEXT NOT NULL,
        at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id INTEGER,
        source TEXT NOT NULL,
        target TEXT NOT NULL,
        relation TEXT NOT NULL
    )
    """,
    # One immutable snapshot per turn (see core/analysis/runs.py) -- ADR 0005. `message_id` is
    # unique: a run is captured exactly once, at `_finalize`, never updated afterwards, so a
    # report or export rendered from this row is unaffected by any later turn in the session.
    """
    CREATE TABLE IF NOT EXISTS analysis_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        message_id INTEGER NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        run TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dead_letter_jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload TEXT,
        error TEXT NOT NULL,
        stack_trace TEXT,
        retry_count INTEGER DEFAULT 0,
        failed_at TEXT DEFAULT (datetime('now')),
        replayed_at TEXT
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
    "CREATE INDEX IF NOT EXISTS idx_analysis_state_message ON analysis_state(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_state_session ON analysis_state(session_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_plan_revisions_message ON plan_revisions(message_id, revision_index)",
    "CREATE INDEX IF NOT EXISTS idx_plan_revisions_session ON plan_revisions(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_nodes_message ON evidence_nodes(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_nodes_session ON evidence_nodes(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_edges_message ON evidence_edges(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_edges_session ON evidence_edges(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_runs_message ON analysis_runs(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_runs_session ON analysis_runs(session_id, created_at)",
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

        self._db_path = Path(self.db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._has_vec = False
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
            
            # Attempt to load sqlite-vec for native vector search
            try:
                conn.enable_load_extension(True)
                import sqlite_vec
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                self._has_vec = True
            except (ImportError, Exception):
                self._has_vec = False
                
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

                if self._has_vec:
                    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_semantic_cache USING vec0(embedding float[384], +cache_key TEXT)")
                    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_trajectories USING vec0(embedding float[384], +session_id TEXT)")
                    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(embedding float[384], +session_id TEXT)")
                
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_trajectories USING fts5(question, code, content='trajectories', content_rowid='rowid')")
            logger.info("SQLite database initialized", path=self.db_path)
        except Exception as e:
            logger.error("Failed to initialize SQLite database", error=str(e))

    # ------------------------------------------------------------------ #
    # Backup and WAL Checkpoint
    # ------------------------------------------------------------------ #
    def backup(self, dest_path: Path | None = None) -> Path:
        """Create a hot, non-blocking backup of the database using the online backup API."""
        import sqlite3 as _sqlite3
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_path or self._db_path.parent / "backups" / f"wizard_{ts}.db"
        dest.parent.mkdir(parents=True, exist_ok=True)
        src_conn = self._connection()
        dst_conn = _sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn, pages=256, sleep=0.01)
        finally:
            dst_conn.close()
        logger.info("database_backup_completed", dest=str(dest), size_bytes=dest.stat().st_size)
        return dest

    def checkpoint(self) -> None:
        """Force a WAL checkpoint to truncate the WAL file."""
        with self._write() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("wal_checkpoint_completed")

    def prune_backups(self, max_count: int = 7) -> int:
        """Remove old backup files, retaining the most recent *max_count*."""
        backup_dir = self._db_path.parent / "backups"
        if not backup_dir.exists():
            return 0
        backups = sorted(backup_dir.glob("wizard_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        for old in backups[max_count:]:
            old.unlink(missing_ok=True)
            removed += 1
        if removed:
            logger.info("backup_pruning_completed", removed=removed, retained=min(len(backups), max_count))
        return removed

    # ------------------------------------------------------------------ #
    # Dead-Letter Queue
    # ------------------------------------------------------------------ #
    def save_dead_letter(self, job_id: str, kind: str, payload: str | None, error: str, stack_trace: str | None, retry_count: int) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dead_letter_jobs (id, kind, payload, error, stack_trace, retry_count) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, kind, payload, error, stack_trace, retry_count),
            )

    def get_dead_letters(self, limit: int = 50) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM dead_letter_jobs WHERE replayed_at IS NULL ORDER BY failed_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def mark_dlq_replayed(self, job_id: str) -> None:
        with self._write() as conn:
            conn.execute("UPDATE dead_letter_jobs SET replayed_at = datetime('now') WHERE id = ?", (job_id,))

    def delete_dead_letter(self, job_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM dead_letter_jobs WHERE id = ?", (job_id,))

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

    def clear_cache_by_session(self, session_id: str) -> None:
        """Remove cache entries for a specific session."""
        try:
            with self._read() as conn:
                rows = conn.execute("SELECT columns FROM schema_registry WHERE session_id = ?", (session_id,)).fetchall()
                if not rows:
                    return
                active_columns = []
                for row in rows:
                    if row["columns"]:
                        active_columns.extend(json.loads(row["columns"]))
                schema_hash = self._schema_hash(active_columns)
            with self._write() as conn:
                conn.execute("DELETE FROM semantic_cache WHERE schema_hash = ?", (schema_hash,))
        except Exception as e:
            logger.error("Failed to clear semantic cache by session", error=str(e))

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
                conn.execute("DELETE FROM analysis_state WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM plan_revisions WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM evidence_nodes WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM evidence_edges WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM analysis_runs WHERE session_id = ?", (session_id,))
        except Exception as e:
            logger.error("Failed to delete session data", error=str(e))

    # ------------------------------------------------------------------ #
    # Analytical state (core/analysis/state.py)
    # ------------------------------------------------------------------ #
    def save_analysis_state(self, session_id: str, message_id: int | None, state: dict[str, Any]) -> int:
        """Persists one turn's `AnalyticalState` snapshot. Returns the new row id, or 0 on failure."""
        try:
            with self._write() as conn:
                cursor = conn.execute(
                    "INSERT INTO analysis_state (session_id, message_id, timestamp, state) VALUES (?, ?, ?, ?)",
                    (session_id, message_id, time.time(), json.dumps(state)),
                )
                return int(cursor.lastrowid or 0)
        except Exception as e:
            logger.error("Failed to save analysis state", error=str(e))
            return 0

    def get_analysis_state(self, message_id: int) -> dict[str, Any] | None:
        """The most recent analytical state snapshot for a given turn's message id."""
        try:
            with self._read() as conn:
                row = conn.execute(
                    "SELECT state FROM analysis_state WHERE message_id = ? ORDER BY id DESC LIMIT 1",
                    (message_id,),
                ).fetchone()
                if row is None or not row["state"]:
                    return None
                return json.loads(row["state"])
        except Exception as e:
            logger.error("Failed to fetch analysis state", error=str(e))
            return None

    def save_plan_revisions(self, session_id: str, message_id: int | None, revisions: list[dict[str, Any]]) -> None:
        """Persists a turn's full plan revision history. Append-only, never replaces a row."""
        if not revisions:
            return
        try:
            with self._write() as conn:
                conn.executemany(
                    "INSERT INTO plan_revisions"
                    " (session_id, message_id, revision_index, text, why, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (session_id, message_id, revision["index"], revision["text"], revision["why"], revision["at"])
                        for revision in revisions
                    ],
                )
        except Exception as e:
            logger.error("Failed to save plan revisions", error=str(e))

    def get_plan_revisions(self, message_id: int) -> list[dict[str, Any]]:
        """One turn's plan revision history, oldest first."""
        try:
            with self._read() as conn:
                rows = conn.execute(
                    "SELECT revision_index, text, why, timestamp FROM plan_revisions"
                    " WHERE message_id = ? ORDER BY revision_index",
                    (message_id,),
                ).fetchall()
                return [
                    {"index": row["revision_index"], "text": row["text"], "why": row["why"], "at": row["timestamp"]}
                    for row in rows
                ]
        except Exception as e:
            logger.error("Failed to fetch plan revisions", error=str(e))
            return []

    # ------------------------------------------------------------------ #
    # Evidence graph (core/analysis/provenance.py)
    # ------------------------------------------------------------------ #
    def save_evidence_graph(self, session_id: str, message_id: int | None, graph: dict[str, Any]) -> None:
        """Persists a turn's evidence graph as rows, one per node and per edge."""
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        if not nodes and not edges:
            return
        try:
            with self._write() as conn:
                if nodes:
                    conn.executemany(
                        "INSERT INTO evidence_nodes"
                        " (session_id, message_id, node_id, kind, label, data, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                session_id,
                                message_id,
                                node["id"],
                                node["kind"],
                                node["label"],
                                json.dumps(node.get("data") or {}),
                                node["at"],
                            )
                            for node in nodes
                        ],
                    )
                if edges:
                    conn.executemany(
                        "INSERT INTO evidence_edges (session_id, message_id, source, target, relation)"
                        " VALUES (?, ?, ?, ?, ?)",
                        [(session_id, message_id, edge["source"], edge["target"], edge["relation"]) for edge in edges],
                    )
        except Exception as e:
            logger.error("Failed to save evidence graph", error=str(e))

    def get_evidence_graph(self, message_id: int) -> dict[str, Any]:
        """One turn's evidence graph, in the shape `EvidenceGraph.from_dict` expects."""
        try:
            with self._read() as conn:
                node_rows = conn.execute(
                    "SELECT node_id, kind, label, data, at FROM evidence_nodes WHERE message_id = ? ORDER BY id",
                    (message_id,),
                ).fetchall()
                edge_rows = conn.execute(
                    "SELECT source, target, relation FROM evidence_edges WHERE message_id = ? ORDER BY id",
                    (message_id,),
                ).fetchall()
                return {
                    "nodes": [
                        {
                            "id": row["node_id"],
                            "kind": row["kind"],
                            "label": row["label"],
                            "data": json.loads(row["data"]),
                            "at": row["at"],
                        }
                        for row in node_rows
                    ],
                    "edges": [
                        {"source": row["source"], "target": row["target"], "relation": row["relation"]}
                        for row in edge_rows
                    ],
                }
        except Exception as e:
            logger.error("Failed to fetch evidence graph", error=str(e))
            return {"nodes": [], "edges": []}

    # ------------------------------------------------------------------ #
    # Analysis runs (core/analysis/runs.py) -- ADR 0005
    # ------------------------------------------------------------------ #
    def save_analysis_run(self, session_id: str, message_id: int, run: dict[str, Any]) -> None:
        """Persists one turn's immutable `AnalysisRun` snapshot. Captured once, at `_finalize`,
        and never updated afterwards -- `message_id` is unique so a later turn cannot overwrite it."""
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO analysis_runs (session_id, message_id, created_at, run) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(message_id) DO NOTHING",
                    (session_id, message_id, time.time(), json.dumps(run)),
                )
        except Exception as e:
            logger.error("Failed to save analysis run", error=str(e))

    def get_analysis_run(self, message_id: int) -> dict[str, Any] | None:
        """The immutable run snapshot for one turn, or `None` if it was never captured."""
        try:
            with self._read() as conn:
                row = conn.execute("SELECT run FROM analysis_runs WHERE message_id = ?", (message_id,)).fetchone()
                return json.loads(row["run"]) if row is not None else None
        except Exception as e:
            logger.error("Failed to fetch analysis run", error=str(e))
            return None

    def get_recent_analysis_runs(self, session_id: str | None, timespan_seconds: int) -> list[dict[str, Any]]:
        """Runs captured within `timespan_seconds`, oldest first -- the same reporting window
        `get_recent_memories` uses, over immutable run snapshots instead of working memory."""
        cutoff = time.time() - timespan_seconds
        try:
            with self._read() as conn:
                sql = "SELECT run FROM analysis_runs WHERE created_at >= ?"
                params: list[Any] = [cutoff]
                if session_id:
                    sql += " AND session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY created_at ASC"
                rows = conn.execute(sql, params).fetchall()
                return [json.loads(row["run"]) for row in rows]
        except Exception as e:
            logger.error("Failed to fetch recent analysis runs", error=str(e))
            return []

    def prune_analysis_runs(self, keep_last: int = 500) -> None:
        """Bounds unbounded growth of the runs table, the same pattern `prune_memories` uses."""
        try:
            with self._write() as conn:
                conn.execute(
                    "DELETE FROM analysis_runs WHERE id NOT IN"
                    " (SELECT id FROM analysis_runs ORDER BY created_at DESC, id DESC LIMIT ?)",
                    (keep_last,),
                )
        except Exception as e:
            logger.error("Failed to prune analysis runs", error=str(e))

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

    def vector_search(self, table: str, query_embedding: list[float], k: int = 10) -> list[tuple[int, float]]:
        """KNN vector search using sqlite-vec, returns (rowid, distance) pairs."""
        if not self._has_vec:
            return []  # caller falls back to in-memory ranking
        import struct
        blob = struct.pack(f'{len(query_embedding)}f', *query_embedding)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT rowid, distance FROM vec_{table} WHERE embedding MATCH ? AND k = ?",
                (blob, k),
            ).fetchall()
        return [(r["rowid"], r["distance"]) for r in rows]

    def fts_search(self, table: str, query: str, limit: int = 20) -> list[dict]:
        """Full-text search using FTS5 BM25 ranking."""
        try:
            with self._read() as conn:
                rows = conn.execute(
                    f"SELECT rowid, rank FROM fts_{table} WHERE fts_{table} MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                ).fetchall()
            return [{"rowid": r["rowid"], "score": -r["rank"]} for r in rows]
        except Exception:
            return []

# Singleton instance
db_mgr = DatabaseManager()
