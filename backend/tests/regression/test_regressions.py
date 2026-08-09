"""Regression tests.

Each test here pins a defect found during the audit of the previous
implementation. The docstrings record what broke and why, so a future change
that reintroduces the behaviour fails with an explanation rather than a bare
assertion.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api.api import app
from src.api.deps import SlidingWindowRateLimiter
from src.config import Settings
from src.core.agent.council import TheCouncil
from src.core.database import DatabaseManager, db_mgr
from src.core.execution import CodeExecutor
from src.core.ingest.loader import json_safe_records, sanitize_columns
from src.core.llm import LLMRole, llm_provider
from src.core.llm.provider import ModelSpec
from src.core.memory import working_memory
from src.core.prompts import TOOLKIT, _toolkit_block
from src.core.rag.retriever import ContextRetriever, mentions_column
from src.core.reporting import ReportingEngine
from src.core.security.code_guard import CodeGuard
from src.core.semantic_cache import semantic_cache
from src.core.session import Session, SessionManager
from src.core.tools.evaluator import Evaluator
from src.core.tools.sandbox import PID_FILE, SandboxSession


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    SessionManager().shutdown()


def test_report_endpoint_does_not_raise_attribute_error() -> None:
    """`ReportingEngine` read `working_memory.memories` as a plain list.

    The SQLite migration removed that attribute while the reporting engine still
    referenced it, so `GET /report` raised AttributeError and returned 500 on
    every single call.
    """
    report = ReportingEngine.generate_executive_summary(timespan_seconds=3600)
    assert isinstance(report, str)
    assert report

    # The compatibility property must also still resolve.
    assert isinstance(working_memory.memories, list)


def test_database_does_not_leak_connections(tmp_path) -> None:
    """`with sqlite3.connect(...)` commits a transaction; it does not close the
    connection. Every call leaked one until garbage collection, and without WAL
    or a busy timeout concurrent writers hit 'database is locked'.
    """
    manager = DatabaseManager(db_path=str(tmp_path / "leak.db"))
    for index in range(50):
        manager.save_feedback(f"task-{index}", "code")

    # One pooled connection per thread, not one per call.
    connection = manager._connection()
    assert manager._connection() is connection

    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert journal_mode.lower() == "wal"
    manager.close()


def test_sanitized_columns_never_collide() -> None:
    """Stripping punctuation mapped distinct headers onto one name.

    `a-b` and `a.b` both became `ab`, producing duplicate column labels that
    later broke Feather serialisation and made column selection ambiguous.
    """
    columns, _ = sanitize_columns(["a-b", "a.b", "a b", "a/b", "a\\b"])
    assert len(columns) == len(set(columns))


def test_preview_records_are_json_serialisable(missing_values_df: pd.DataFrame) -> None:
    """`df.replace({float('nan'): None})` did not cover every dtype, so NaN and
    Inf reached the JSON encoder and `/data/preview` returned 500.
    """
    json.dumps(json_safe_records(missing_values_df))


def test_evaluator_ignores_the_word_error_in_prose() -> None:
    """Scoring matched the bare substring "Error", so any output mentioning it —
    including a column named `error_rate` — lost 50 points.
    """
    scored = Evaluator.score_execution("Mean error_rate is 0.01, variance is low.")
    assert scored["score"] >= 90


def test_sandbox_interrupt_targets_the_daemon_not_pid_one() -> None:
    """`interrupt()` sent `kill -2 1`, but PID 1 is the container's `sleep`
    command, so pressing Stop destroyed the sandbox instead of the running cell.
    The daemon now writes its own PID and is signalled directly.
    """

    class RecordingContainer:
        def __init__(self) -> None:
            self.commands: list[object] = []

        def exec_run(self, command):
            self.commands.append(command)
            return type("Result", (), {"exit_code": 0})()

    session = SandboxSession.__new__(SandboxSession)
    session.container = RecordingContainer()
    session.id = "test"

    assert session.interrupt() is True

    issued = " ".join(str(part) for part in session.container.commands[0])
    assert PID_FILE in issued, "the daemon's own PID file must be consulted"
    assert "kill -INT" in issued
    assert "kill -2 1" not in issued, "signalling PID 1 kills the container, not the cell"


def test_sandbox_run_code_takes_no_dataframe_payload() -> None:
    """`run_code(code, df_bytes, ...)` accepted a Feather-encoded frame and then
    ignored it, so every execution paid a full DataFrame serialisation for
    nothing. The dataset travels through the bind mount instead.
    """
    import inspect

    signature = inspect.signature(SandboxSession.run_code)
    assert "df_bytes" not in signature.parameters


def test_sandbox_daemon_source_is_valid_python() -> None:
    """The daemon lives in a string literal and only ever runs inside Docker, so
    a syntax error in it is invisible to every other test and to the type
    checker — it would surface as a container that silently never accepts
    connections.
    """
    import ast

    from src.core.tools.daemon import render_daemon

    for allow_pip in (True, False):
        for mem_bytes in (0, 1 << 30):
            rendered = render_daemon(
                pid_file=PID_FILE,
                allow_pip=allow_pip,
                workspace="/workspace",
                mem_bytes=mem_bytes,
            )
            ast.parse(rendered)  # must not raise

    # The placeholders must all be substituted; a stray one would be a runtime
    # NameError inside the container.
    assert "%(" not in rendered

    # A Windows workspace path reaches the daemon as a string literal, so an
    # unescaped backslash would be a syntax error in the rendered source. Checked
    # by round-trip, not by looking for forward slashes: the earlier fix relied
    # on `Path.as_posix()`, which only rewrites separators when it runs *on*
    # Windows, so the daemon was unparseable on Linux and macOS.
    path = "C:\\Users\\a\\workspace\\sessions\\abc"
    windows = render_daemon(workspace=path)
    ast.parse(windows)
    line = next(ln for ln in windows.splitlines() if ln.startswith("WORKSPACE"))
    namespace: dict = {}
    exec(line, namespace)  # noqa: S102 - evaluating source this test just generated
    assert namespace["WORKSPACE"] == path


def test_rate_limiter_evicts_idle_clients() -> None:
    """The old limiter used an unbounded `defaultdict(list)` that was never
    swept, so every IP that ever connected stayed resident for the process
    lifetime.
    """
    limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=1, max_keys=10)
    for index in range(200):
        limiter.allow(f"client-{index}")

    assert len(limiter._hits) <= 10


def test_rate_limiter_blocks_past_the_threshold() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60)
    assert all(limiter.allow("client") for _ in range(3))
    assert not limiter.allow("client")


def test_rate_limiter_is_thread_safe() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=1000, window_seconds=60)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(200):
                limiter.allow("shared")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors


def test_cors_credentials_are_disabled_for_wildcard_origins() -> None:
    """`allow_origins=["*"]` with `allow_credentials=True` is rejected by every
    browser and is a spec violation. The two settings are now resolved together.
    """
    wildcard = Settings(CORS_ALLOW_ORIGINS="*")
    assert wildcard.cors_allow_credentials is False

    explicit = Settings(CORS_ALLOW_ORIGINS="http://localhost:3000")
    assert explicit.cors_allow_credentials is True


def test_sessions_do_not_share_a_sandbox_namespace() -> None:
    """A single global `state` dict plus one shared container meant a second
    user could read the first user's variables.
    """
    manager = SessionManager()
    first = manager.create()
    second = manager.create()

    assert first.id != second.id
    assert first.workspace != second.workspace
    assert first.executor.session_id != second.executor.session_id

    manager.drop(first.id)
    manager.drop(second.id)


def test_import_healing_runs_on_parseable_code() -> None:
    """Import healing was gated behind a SyntaxError, but a missing import is a
    runtime NameError and parses fine — so the branch could never fire.
    """
    parses, code = CodeGuard.repair("total = pd.Series([1, 2]).sum()\nprint(total)")
    assert parses
    assert "import pandas as pd" in code


def test_guard_blocks_open_self_traversal() -> None:
    """`open.__self__` reaches the builtins module; neither `__self__` nor
    `__dict__` was in the original attribute denylist.
    """
    assert not CodeGuard.scan("print(open.__self__.__dict__)").ok


def test_cleaning_path_is_guarded(loaded_session: Session) -> None:
    """`ScientificAgent.clean_dataset` called the builtin `exec()` directly in
    the API process, with no guard and no sandbox, on every upload — while the
    uploaded file's column names were already inside the prompt.
    """
    executor = CodeExecutor(loaded_session.id)
    result = executor.execute("import os\nos.system('id')", loaded_session.df)

    assert result.blocked
    assert not result.ok


def test_inprocess_fallback_reports_that_it_is_degraded(loaded_session: Session) -> None:
    """The in-process interpreter still runs code, but the caller must be able
    to tell that no isolation was available."""
    result = CodeExecutor(loaded_session.id).execute("print(df.shape)", loaded_session.df)

    assert result.ok
    assert result.sandboxed is False
    assert result.isolation == "none"
    assert any("EXECUTION_BACKEND=host" in warning for warning in result.warnings)


def test_syntax_errors_are_retryable_not_policy_violations(loaded_session: Session) -> None:
    """Blocking and malformed output used to be conflated: both terminated the
    run as `completed`, so the model never got a chance to fix its own typo.
    """
    result = CodeExecutor(loaded_session.id).execute("print('unterminated", loaded_session.df)

    assert not result.ok
    assert result.retryable_error
    assert not result.blocked


def test_provider_is_part_of_the_llm_client_cache_key() -> None:
    """`ModelSpec.cache_key` covered only (provider, model, temperature, ...).

    Adding a second backend made that ambiguous: the same model name served by
    Ollama and by LM Studio produced the same key, so whichever endpoint was
    contacted first answered for both. The endpoint is now part of the key.
    """
    spec = llm_provider.resolve(LLMRole.WORKER, model="shared-name", provider="ollama")
    other = llm_provider.resolve(LLMRole.WORKER, model="shared-name", provider="lmstudio")

    assert spec.base_url != other.base_url
    assert spec.cache_key() != other.cache_key()


def test_lmstudio_base_url_accepts_the_form_the_app_displays() -> None:
    """LM Studio shows its endpoint as `http://localhost:1234/v1`, so that is
    what users paste. Discovery needs the bare root, and the earlier code would
    have built `.../v1/api/v0/models`, which 404s.
    """
    parsed = Settings(LMSTUDIO_BASE_URL="http://localhost:1234/v1")

    assert parsed.LMSTUDIO_BASE_URL == "http://localhost:1234"
    assert parsed.provider_openai_base_url("lmstudio") == "http://localhost:1234/v1"


def test_council_honours_the_session_model_choice() -> None:
    """Specialists called `acomplete` with no `model=`, so every review ran on
    the configured default even after the user had picked something else.
    """
    import inspect

    from src.core.agent.council import SpecialistAgent

    signature = inspect.signature(SpecialistAgent._ask)
    assert "models" in signature.parameters

    signature = inspect.signature(TheCouncil.adjudicate)
    assert "models" in signature.parameters


def test_clearing_the_semantic_cache_also_clears_the_in_process_layer() -> None:
    """`add()` writes to the SQLite table *and* to the in-process exact-match
    cache, but the suite's teardown only truncated the table. A later test
    asking the same question hit the stale in-memory entry, took the cache-hit
    path and never called the LLM it had scripted -- an order-dependent failure
    that passed whenever the file was run on its own.
    """
    columns = ["A", "B"]
    semantic_cache.add("how many rows", columns, "print(len(df))")
    assert semantic_cache.lookup("how many rows", columns) is not None

    semantic_cache.clear()

    assert semantic_cache.lookup("how many rows", columns) is None
    assert db_mgr.get_cache_entries(columns) == []


# --------------------------------------------------------------------------- #
# The agentic rewrite
# --------------------------------------------------------------------------- #
def test_a_short_column_name_is_not_matched_inside_an_unrelated_word() -> None:
    """Column relevance used a substring test: `str(column).lower() in query`.

    A column named `C` therefore matched inside "check", `id` matched inside
    "provide", and `n` matched everywhere. Short column names are extremely
    common, so nearly every column reported as "explicitly named by the user"
    and was pinned into the prompt — which defeated the whole point of the
    column budget on exactly the wide frames it exists for.
    """
    assert mentions_column("check the nulls", "C") is False
    assert mentions_column("provide a summary", "id") is False
    assert mentions_column("plot revenue by region", "revenue") is True
    assert mentions_column("what is C", "C") is True
    # Word boundaries, not whitespace: punctuation still delimits.
    assert mentions_column("group by `region`,", "region") is True


def test_the_column_budget_still_budgets_on_a_wide_frame() -> None:
    """The consequence of the bug above, measured end to end."""
    frame = pd.DataFrame({name: [1] for name in ["a", "b", "c", "id", "n"] + [f"col_{i}" for i in range(60)]})
    retriever = ContextRetriever()

    selected, truncated = retriever.select_columns("check and provide an analysis of col_7", frame, max_columns=10)

    assert truncated
    assert len(selected) <= 10
    assert "col_7" in selected


def test_fast_mode_does_not_pay_for_verification() -> None:
    """`budget_for` applied the iteration count for `fast` but left the tier's
    verification flag alone, so the cheapest mode silently ran a second code
    generation *and* a second execution — the most expensive thing a turn does.
    """
    budget = Settings().budget_for("fast", "70B")
    assert budget.iterations == 1
    assert not budget.allow_verification
    assert not budget.allow_reflection


def test_no_model_name_is_hardcoded_in_the_defaults() -> None:
    """MODEL_NAME/WORKER_MODEL_NAME defaulted to two specific Ollama tags.

    That made those exact models load-bearing: the tag 404s on LM Studio and on
    every gateway, so switching provider failed with an opaque error until the
    user also edited their .env. Empty means "use what this provider has".
    """
    fresh = Settings()
    assert fresh.MODEL_NAME == ""
    assert fresh.WORKER_MODEL_NAME == ""
    assert fresh.VISION_MODEL_NAME == ""


def test_an_unresolvable_model_names_the_endpoint_it_tried() -> None:
    """ "No LLM client available" alone is undebuggable, and with empty defaults
    the model name can now legitimately be blank — so the message has to say
    that discovery found nothing rather than quoting an empty string."""
    spec = ModelSpec(provider="lmstudio", model="", temperature=0.0, max_tokens=10, num_ctx=10, base_url="http://x/v1")
    message = llm_provider._unavailable_message(spec)

    assert "http://x/v1" in message
    assert "lmstudio" in message
    assert "discovery" in message.lower()


def test_removing_a_table_drops_it_from_the_sandbox_namespace(session: Session) -> None:
    """Every session table is preloaded into the daemon's `tables` dict. Without
    an explicit reload the removed frame stayed queryable from generated code
    after the user deleted it."""
    session.add_dataset("orders.csv", pd.DataFrame({"id": [1]}))
    session.add_dataset("customers.csv", pd.DataFrame({"id": [1]}))

    session.remove_dataset("orders.csv")

    assert "orders" not in session.tables
    assert not (session.workspace / "tables" / "orders.feather").exists()


def test_the_toolkit_block_does_not_advertise_what_is_not_installed(monkeypatch) -> None:
    """The worker prompt lists the libraries available. In the Docker-less
    fallback the code runs in the API process, which has a much smaller set than
    the sandbox image — advertising duckdb there produced a confident
    ImportError and burned a correction retry."""
    monkeypatch.setattr("src.core.tools.sandbox.sandbox_pool._client_checked", True, raising=False)
    monkeypatch.setattr("src.core.tools.sandbox.sandbox_pool._client", None, raising=False)

    block = _toolkit_block()

    assert "Do not import a library that is not listed" in block
    for area, _, modules in TOOLKIT:
        if "duckdb" in modules and not _module_present("duckdb"):
            assert area not in block


def _module_present(name: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def test_a_write_target_is_never_a_hardcoded_container_path(monkeypatch, session: Session) -> None:
    """Generated code is handed a path to write to. `/workspace` is only real
    inside a container: on the local subprocess backend it resolves to a
    directory that does not exist, pandas raises `OSError`, and the caller
    reports a generic failure.

    That silently disabled semantic cleaning on upload and CSV export of a
    variable on every Docker-less install. It was invisible to the suite
    because the in-process backend used by CI happens to tolerate the very
    same path, and it was found only by running the app.
    """
    from src.core.tools import runtime as runtime_backend

    monkeypatch.setattr(runtime_backend, "active_backend", lambda: "host")

    target = runtime_backend.workspace_path(session.id, "cleaned.csv")

    assert not target.startswith("/workspace/")
    assert target.endswith("/cleaned.csv")
    # The parent has to be a directory that exists, or the write fails exactly
    # as it did before.
    assert Path(target).parent.is_dir()


def test_a_container_still_gets_the_container_path(monkeypatch) -> None:
    """The fix must not break the Docker backend, where `/workspace` is the
    bind-mount destination and the session directory does not exist."""
    from src.core.tools import runtime as runtime_backend

    monkeypatch.setattr(runtime_backend, "active_backend", lambda: "docker")

    assert runtime_backend.workspace_path("any-session", "plot.html") == "/workspace/plot.html"


def test_the_prompt_tells_the_model_the_root_its_runtime_actually_has(monkeypatch, session: Session) -> None:
    """The related-tables block told the model to `pd.read_csv('/workspace/...')`
    regardless of backend, so following the instruction correctly still failed
    without Docker."""
    from src.core.prompts import _related_tables
    from src.core.tools import runtime as runtime_backend

    monkeypatch.setattr(runtime_backend, "active_backend", lambda: "host")
    session.add_dataset("orders.csv", pd.DataFrame({"id": [1], "region": ["N"]}))
    session.add_dataset("regions.csv", pd.DataFrame({"region": ["N"], "manager": ["x"]}))

    block = _related_tables("join orders to regions", session.id, ["region"])

    if block:
        assert "'/workspace/<filename>'" not in block


def test_the_logger_configures_on_an_interactive_terminal(monkeypatch) -> None:
    """Starting the server in a real terminal raised AttributeError on boot.

    `ConsoleRenderer` lives in `structlog.dev`, and the config asked for
    `structlog.processors.ConsoleRenderer`. A ternary only evaluates the branch
    it takes, and that branch is chosen by `sys.stdout.isatty()` — so every
    automated run (CI, and anything redirecting output to a file) took the JSON
    branch and never touched the broken name. The first person to run
    `uvicorn` in a terminal got a crash the whole suite was blind to.
    """
    import io
    import sys

    from src.utils.logging import configure_logger, logger

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdout", _Tty())
    try:
        configure_logger()
        logger.info("hello from a terminal", answer=42)
    finally:
        # Restore the non-tty configuration for the rest of the suite.
        monkeypatch.undo()
        configure_logger()


def test_local_only_never_constructs_a_cloud_client(monkeypatch) -> None:
    """The mechanism behind the local-first promise, pinned at its choke point.

    Before Milestone 1 "your data stays local" was a property of how somebody
    happened to configure their .env: a cloud provider assigned to a role was
    simply used, and the prompt — sample rows and all — went to it. The check now
    lives in `LLMProvider.resolve`, which every one of the nine call sites passes
    through, precisely so a session pinning its own per-role provider cannot
    route around it.
    """
    from src.core.data_mode import check_provider
    from src.core.llm.provider import DataModeViolation
    from src.providers import CLOUD_PROVIDERS

    attempted: list[str] = []
    monkeypatch.setattr(llm_provider, "_build_client", lambda spec: attempted.append(spec.provider))

    for provider in sorted(CLOUD_PROVIDERS):
        for role in (LLMRole.MANAGER, LLMRole.WORKER, LLMRole.VISION):
            try:
                llm_provider.resolve(role, model="anything", provider=provider, data_mode="local-only")
            except DataModeViolation:
                continue
            raise AssertionError(f"{provider} was resolvable for {role.value} under local-only")

    assert attempted == []
    assert check_provider("local-only", "ollama") is None


def test_switching_to_local_only_drops_a_pinned_cloud_role(client) -> None:
    """Leaving the assignment in place would mean the next question failed rather
    than ran, with nothing on screen explaining why — the setting the user just
    chose to be safer would read as having broken the app."""
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    client.post("/api/data-mode", json={"mode": "hybrid"}, headers=headers)
    assert (
        client.post(
            "/api/models",
            json={"manager_provider": "anthropic", "manager": "claude-sonnet-4-5"},
            headers=headers,
        ).status_code
        == 200
    )

    client.post("/api/data-mode", json={"mode": "local-only"}, headers=headers)
    models = client.get("/api/session", headers=headers).json()["models"]
    assert models["manager_provider"] is None


def test_a_forbidden_provider_is_refused_when_it_is_chosen(client) -> None:
    """A 409 naming the mode is actionable. Accepting the choice and failing at
    run time three clicks later is not."""
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    client.post("/api/data-mode", json={"mode": "local-only"}, headers=headers)
    response = client.post("/api/models", json={"worker_provider": "openai"}, headers=headers)

    assert response.status_code == 409
    assert "local-only" in response.json()["detail"]


def test_the_cost_readout_is_absent_rather_than_zero_under_local_only(client) -> None:
    """`$0.00` reads as a computed number. Under local-only nothing was spent and
    nothing could be, and the client needs to be able to say that instead."""
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    client.post("/api/data-mode", json={"mode": "local-only"}, headers=headers)
    usage = client.get("/api/usage", headers=headers).json()

    assert usage["local_only"] is True
    assert usage["cost_usd"] is None
    assert usage["any_cloud"] is False


def test_an_unpriced_cloud_model_never_reports_a_guessed_cost() -> None:
    """The grounding layer's rule applied to the meter: report what is known,
    say what is not, invent nothing."""
    from src.core.llm.usage import SessionUsage, TokenUsage

    usage = SessionUsage()
    usage.add("custom_gateway", "someones-private-deployment", "manager", TokenUsage(10_000, 2_000))
    totals = usage.to_dict()

    assert totals["total_tokens"] == 12_000
    assert totals["cost_usd"] is None
    assert totals["unpriced_models"] == ["someones-private-deployment"]


def test_a_per_dataset_policy_survives_to_the_prompt(client) -> None:
    """The spec asks for the cloud-data policy to be settable per source, and a
    setting that does not reach the prompt builder is decoration."""
    import io

    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}
    client.post("/api/data-mode", json={"mode": "hybrid", "schema_only": False}, headers=headers)

    csv = io.BytesIO(b"region,salary\nNorthwood,50000\nEastvale,61000\n")
    upload = client.post(
        "/api/datasets?clean=false",
        files={"file": ("payroll.csv", csv, "text/csv")},
        headers=headers,
    )
    assert upload.status_code == 200

    mode = client.put("/api/data-mode/dataset/payroll.csv", json={"schema_only": True}, headers=headers).json()
    assert mode["per_dataset"] == {"payroll.csv": True}

    from src.core.agent.orchestrator import orchestrator
    from src.core.session import session_manager

    session = session_manager.get(session_id)
    assert session is not None
    session.models.manager_provider = "anthropic"
    assert orchestrator._redact_for(session, "manager") is True, "the override did not reach the prompt decision"

    cleared = client.delete("/api/data-mode/dataset/payroll.csv", headers=headers).json()
    assert cleared["per_dataset"] == {}
    assert orchestrator._redact_for(session, "manager") is False


def test_workspace_files_are_not_reachable_across_sessions(client) -> None:
    """H-9 (GitHub #88): `/api/workspace/files` and `/api/workspace/file/{path}`
    must require the *caller's own* session, not merely *a* session.

    Before the fix, anyone who obtained or guessed a session id could list and
    download another session's files -- including the raw proprietary dataset --
    because the only check was that a session existed at all.
    """
    import io

    owner = client.post("/api/session").json()["session_id"]
    owner_headers = {"X-Session-Id": owner}
    csv = io.BytesIO(b"secret,value\nrow,1\n")
    upload = client.post(
        "/api/datasets?clean=false",
        files={"file": ("dataset.csv", csv, "text/csv")},
        headers=owner_headers,
    )
    assert upload.status_code == 200

    intruder = client.post("/api/session").json()["session_id"]
    intruder_headers = {"X-Session-Id": intruder}

    listing = client.get("/api/workspace/files", headers=intruder_headers).json()
    assert not any(entry["name"] == "dataset.csv" for entry in listing["files"]), (
        "the intruder's own (empty) session must not surface the owner's files"
    )

    download = client.get("/api/workspace/file/dataset.csv", headers=intruder_headers)
    assert download.status_code == 404, "the intruder's session has no such file of its own"

    forged = client.get("/api/workspace/file/dataset.csv", headers={"X-Session-Id": "not-a-real-session-id"})
    assert forged.status_code == 404, "an unknown session id must be rejected, not silently minted a fresh session"

    owned = client.get("/api/workspace/file/dataset.csv", headers=owner_headers)
    assert owned.status_code == 200


def test_a_policy_cannot_be_set_for_a_dataset_that_is_not_loaded(client) -> None:
    """A silently-stored override for a name that does not exist would read as
    protection that is not actually in force."""
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    response = client.put("/api/data-mode/dataset/ghost.csv", json={"schema_only": True}, headers=headers)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Permission profile (Milestone 2)
# --------------------------------------------------------------------------- #
def test_the_data_mode_outranks_the_permission_profile(client) -> None:
    """`auto-approve` must not be able to consent past `local-only`.

    The two dials answer adjacent questions and rank in one order only: the mode
    decides what is possible at all, the profile decides what is asked about
    among what already is. Letting a profile reach past the mode would turn the
    one hard boundary in the product into a preference.
    """
    from src.core.data_mode import tool_allowed
    from src.core.permissions import PermissionState

    assert PermissionState(profile="auto-approve").ruling_for("network") == "allow"
    assert tool_allowed("local-only", "web_search") is False


def test_auto_approve_never_covers_write_back(client) -> None:
    """The one category no blanket profile may include.

    Write-back changes data outside this machine. The spec is explicit that it is
    enabled per connection, once, deliberately — never implied by "connect" and
    never by choosing a convenient profile.
    """
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    body = client.post("/api/permissions", json={"profile": "auto-approve"}, headers=headers).json()
    rulings = {row["key"]: row["ruling"] for row in body["categories"]}

    assert rulings["library_install"] == "allow"
    assert rulings["db_write"] == "ask", "a blanket profile reached the write-back category"


def test_write_back_cannot_be_allowed_through_the_api(client) -> None:
    """Rejected at the edge, not silently clamped, so the UI can explain why."""
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    response = client.post(
        "/api/permissions",
        json={"profile": "custom", "categories": {"db_write": "allow"}},
        headers=headers,
    )
    assert response.status_code == 400
    assert "per connection" in response.json()["detail"]


def test_loosening_then_tightening_the_profile_drops_earlier_grants(client) -> None:
    """A grant is consent under the rules in force when it was given.

    Carrying grants across a tightening would mean choosing a stricter profile
    changed nothing about what the agent was still free to do — the setting the
    user picked to be safer would be decorative.
    """
    from src.core.session import session_manager

    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}
    client.post("/api/permissions", json={"profile": "auto-approve"}, headers=headers)

    session = session_manager.get(session_id)
    assert session is not None
    session.permissions.grant("library_install", "lifelines")
    session.permissions.allow_root("/data/reports")

    client.post("/api/permissions", json={"profile": "ask-always"}, headers=headers)

    assert not session.permissions.granted("library_install", "lifelines")
    assert session.permissions.extra_roots == ()


def test_the_permission_profile_is_reported_on_the_session(client) -> None:
    """The UI reads it from one place, so it has to be in `describe()`."""
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    body = client.get("/api/session", headers=headers).json()
    assert body["permissions"]["profile"] == "ask-always"


def test_liveness_tracks_what_actually_has_a_call_site(client) -> None:
    """A category is reported live only once something reaches it.

    Reporting one as live before it ships would imply a capability that does not
    exist -- the same class of mistake as a toolkit entry advertising a library
    the runtime does not have. Milestone 4 gave `db_connect` and `db_write` real
    call sites, so they flipped in the same change that added them. `tool_use`
    has none yet and must stay false, or this assertion stops testing anything.
    """
    session_id = client.post("/api/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}

    body = client.get("/api/permissions", headers=headers).json()
    live = {row["key"]: row["live"] for row in body["categories"]}

    assert live["library_install"] is True
    assert live["db_connect"] is True
    assert live["db_write"] is True
    assert live["tool_use"] is False


def test_a_skill_file_cannot_state_its_own_provenance(tmp_path: Path) -> None:
    """A SKILL.md declaring `source_url`/`pinned_sha` must not surface them.

    Milestone 5 read both keys out of the frontmatter, which was harmless while
    the only writer was this machine. Milestone 6 fetches these files from
    strangers' repositories, and at that point a claim written by the payload is
    not provenance: a hostile skill could name any commit in any repository it
    liked, and the UI would render an unearned badge beside instruction text it
    has no basis for trusting.

    The loader now ignores both keys; `install_index.overlay` stamps them on
    afterwards from a file this machine wrote. This pins the ignoring, because
    re-adding two lines to `load_skill` is the most plausible way it comes back.
    """
    from src.core.skills.loader import load_skill
    from src.core.skills.spec import SkillLayer

    directory = tmp_path / "spoofed"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\n"
        "name: spoofed\n"
        "description: Looks official\n"
        "source_url: https://github.com/trusted/vendor\n"
        "pinned_sha: 0123456789abcdef0123456789abcdef01234567\n"
        "---\n\nInstructions.\n",
        encoding="utf-8",
    )

    skill = load_skill(directory, SkillLayer.USER, embed=False)

    assert skill.source_url is None
    assert skill.pinned_sha is None
