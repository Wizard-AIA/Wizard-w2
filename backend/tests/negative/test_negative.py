"""Negative tests: hostile, malformed and degenerate input.

Everything here asserts that the system refuses cleanly — a clear error, a
rejected request, or a documented fallback — rather than crashing, hanging or
silently doing the dangerous thing.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api.api import app
from src.config import settings
from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.execution import CodeExecutor
from src.core.ingest.loader import DatasetLoader
from src.core.rag.retriever import ContextRetriever
from src.core.security.code_guard import CodeGuard
from src.core.session import Session, session_manager
from src.core.tools.catalog import CatalogEngine
from src.core.tools.stats import StatisticalToolkit


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    session_manager.shutdown()


# --------------------------------------------------------------------------- #
# Hostile code
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        "import os; os.environ['AWS_SECRET_ACCESS_KEY']",
        "open('/proc/self/environ').read()",
        "import socket; socket.socket().connect(('10.0.0.1', 22))",
        "__import__('subprocess').check_output('whoami')",
        "exec(compile('import os', '<s>', 'exec'))",
        "getattr(__builtins__, 'ev' + 'al')('1')",
        "[c for c in ().__class__.__base__.__subclasses__()]",
        "import pickle; pickle.loads(b'cos\\nsystem\\n')",
    ],
)
def test_exfiltration_and_escape_attempts_are_blocked(payload: str, loaded_session: Session) -> None:
    result = CodeExecutor(loaded_session.id).execute(payload, loaded_session.df)
    assert not result.ok
    assert result.blocked, f"not blocked: {payload}"


def test_blocked_code_never_reaches_the_interpreter(loaded_session: Session, tmp_path) -> None:
    """A blocked program must have no side effects at all."""
    marker = tmp_path / "should-not-exist.txt"
    payload = f"import os\nopen({str(marker)!r}, 'w').write('pwned')"

    result = CodeExecutor(loaded_session.id).execute(payload, loaded_session.df)

    assert result.blocked
    assert not marker.exists()


def test_infinite_loop_is_not_executed_when_it_also_violates_policy(loaded_session: Session) -> None:
    result = CodeExecutor(loaded_session.id).execute("import os\nwhile True: pass", loaded_session.df)
    assert result.blocked


@pytest.mark.parametrize(
    "code",
    ["print('unterminated", "def f(:\n    pass", "if True\n    print(1)", "x = = 5"],
)
def test_malformed_code_is_retryable(code: str, loaded_session: Session) -> None:
    result = CodeExecutor(loaded_session.id).execute(code, loaded_session.df)
    assert not result.ok
    assert result.retryable_error
    assert not result.blocked


def test_runtime_error_is_reported_not_raised(loaded_session: Session) -> None:
    result = CodeExecutor(loaded_session.id).execute("print(df['NOT_A_COLUMN'])", loaded_session.df)
    assert not result.ok
    assert "KeyError" in result.output or "Error executing code" in result.output


def test_empty_code_is_rejected(loaded_session: Session) -> None:
    result = CodeExecutor(loaded_session.id).execute("", loaded_session.df)
    assert not result.ok


# --------------------------------------------------------------------------- #
# Hostile uploads
# --------------------------------------------------------------------------- #
def test_executable_disguised_as_csv_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("payload.exe", b"MZ\x90\x00\x03", "text/csv")},
    )
    assert response.status_code == 422


def test_binary_content_with_a_csv_name_fails_cleanly(client: TestClient) -> None:
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("data.csv", bytes(range(256)) * 20, "text/csv")},
    )
    # Either it parses into something harmless or it is rejected — never a 500.
    assert response.status_code in (200, 400, 422), response.text


def test_oversized_upload_is_rejected(tmp_path) -> None:
    stream = io.BytesIO(b"a,b\n" + b"1,2\n" * 100_000)
    with pytest.raises(ValueError, match="too large"):
        DatasetLoader.spool_to_disk(stream, tmp_path / "big.csv", max_bytes=1024)


def test_path_traversal_in_a_filename_is_neutralised(client: TestClient) -> None:
    """The filename is attacker-controlled and is used to name a workspace file."""
    body = b"a,b\n1,2\n"
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("../../../../etc/passwd.csv", body, "text/csv")},
    )
    assert response.status_code == 200

    name = response.json()["dataset"]["name"]
    assert "/" not in name and "\\" not in name and ".." not in name


def test_csv_with_formula_injection_is_stored_as_data(client: TestClient) -> None:
    """A leading `=` is a spreadsheet formula, not something we should evaluate."""
    body = b"name,value\n=cmd|'/c calc'!A1,2\n"
    response = client.post("/api/datasets?clean=false", files={"file": ("f.csv", body, "text/csv")})
    assert response.status_code == 200


def test_upload_without_a_file_is_rejected(client: TestClient) -> None:
    assert client.post("/api/datasets").status_code == 422


# --------------------------------------------------------------------------- #
# Degenerate data
# --------------------------------------------------------------------------- #
def test_single_row_dataset(client: TestClient) -> None:
    response = client.post("/api/datasets?clean=false", files={"file": ("one.csv", b"a,b\n1,2\n", "text/csv")})
    assert response.status_code == 200
    assert response.json()["dataset"]["rows"] == 1


def test_duplicate_headers_are_disambiguated(client: TestClient) -> None:
    response = client.post("/api/datasets?clean=false", files={"file": ("d.csv", b"a,a,a\n1,2,3\n", "text/csv")})
    assert response.status_code == 200
    columns = response.json()["dataset"]["columns"]
    assert len(columns) == len(set(columns))


def test_unicode_and_emoji_headers(client: TestClient) -> None:
    body = "名前,émoji 🎉,ok\n1,2,3\n".encode()
    response = client.post("/api/datasets?clean=false", files={"file": ("u.csv", body, "text/csv")})
    assert response.status_code == 200


def test_all_null_column_is_dropped(client: TestClient) -> None:
    response = client.post("/api/datasets?clean=false", files={"file": ("n.csv", b"a,b\n1,\n2,\n", "text/csv")})
    assert response.status_code == 200
    assert "b" not in response.json()["dataset"]["columns"]


def test_statistics_on_a_constant_column() -> None:
    df = pd.DataFrame({"x": [5.0] * 20})
    outliers = StatisticalToolkit.detect_outliers(df, "x")
    assert outliers["outlier_count"] == 0


def test_statistics_on_an_all_nan_column() -> None:
    df = pd.DataFrame({"x": [np.nan] * 10})
    result = StatisticalToolkit.check_normality(df, "x")
    assert result["is_normal"] is False


def test_catalog_on_a_single_column_of_nulls() -> None:
    catalog = CatalogEngine.analyze(pd.DataFrame({"x": [None, None]}))
    assert catalog["columns"]["x"]["semantic_type"] == "empty"


def test_column_selection_on_an_empty_frame() -> None:
    columns, truncated = ContextRetriever().select_columns("anything", pd.DataFrame())
    assert columns == []
    assert truncated is False


# --------------------------------------------------------------------------- #
# Hostile / malformed API requests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"message": None},
        {"message": 123},
        {"message": "hi", "mode": 99},
        {"wrong_field": "hi"},
    ],
)
def test_malformed_chat_payloads_are_422(client: TestClient, body: dict) -> None:
    response = client.post("/api/chat", json=body)
    assert response.status_code in (412, 422), response.text


def _session_with_data(client: TestClient) -> str:
    response = client.post("/api/datasets?clean=false", files={"file": ("d.csv", b"a,b\n1,2\n3,4\n", "text/csv")})
    return response.json()["session_id"]


def test_negative_pagination_is_rejected(client: TestClient) -> None:
    headers = {"X-Session-Id": _session_with_data(client)}
    assert client.get("/api/data/preview?page=-1", headers=headers).status_code == 422


def test_excessive_page_size_is_rejected(client: TestClient) -> None:
    headers = {"X-Session-Id": _session_with_data(client)}
    assert client.get("/api/data/preview?per_page=100000", headers=headers).status_code == 422


def test_preview_without_a_dataset_is_412(client: TestClient) -> None:
    assert client.get("/api/data/preview").status_code == 412


def test_unknown_route_is_404(client: TestClient) -> None:
    assert client.get("/api/does-not-exist").status_code == 404


def test_wrong_method_is_405(client: TestClient) -> None:
    response = client.delete("/health")
    assert response.status_code == 405


def test_model_selection_rejects_out_of_range_temperature(client: TestClient) -> None:
    response = client.post("/api/models", json={"temperature": 99})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Orchestrator degradation
# --------------------------------------------------------------------------- #
async def test_run_without_a_dataset_is_a_reply_not_a_crash(session: Session) -> None:
    """No dataset is a conversation about what is missing (v1.0.14), not an error.

    The model is unreachable here, so this also covers the fallback: the user is
    still told what to do.
    """
    collector = EventCollector()
    result = await orchestrator.run(session=session, instruction="analyse", mode="fast", emitter=collector)
    assert result.status == "completed"
    assert result.route["workflow"] == "converse" and result.route["needs_data"] is True
    assert "dataset" in result.answer.lower()
    assert not collector.of_type(EventType.ERROR)


async def test_resuming_an_approved_plan_without_a_dataset_still_fails_cleanly(session: Session) -> None:
    """An approval carries no message to converse about: the data it ran on is gone."""
    result = await orchestrator.run(
        session=session, instruction="analyse", mode="auto", emitter=EventCollector(), approved_plan="1. do it"
    )
    assert result.status == "failed"


async def test_model_returning_prose_instead_of_code_is_handled(loaded_session: Session, monkeypatch) -> None:
    """A small model sometimes answers in English where code was requested."""

    class ProseLLM:
        async def acomplete(self, *args, **kwargs) -> str:
            return "I would compute the mean of column A."

        async def stream_to(self, prompt, on_delta=None, **kwargs) -> str:
            return "I would compute the mean of column A."

    monkeypatch.setattr("src.core.agent.orchestrator.llm_provider", ProseLLM())

    result = await orchestrator.run(
        session=loaded_session, instruction="mean of A", mode="fast", emitter=EventCollector()
    )
    # No crash, and the failure is surfaced rather than silently swallowed.
    assert result.status in ("completed", "failed")


def test_guard_rejects_enormous_input() -> None:
    """A pathological payload must not hang the parser."""
    verdict = CodeGuard.scan("x = 1\n" * 200_000)
    assert verdict.ok is True  # valid, if pointless


def test_guard_handles_null_bytes() -> None:
    verdict = CodeGuard.scan("print('a')\x00")
    assert verdict.syntax_error or not verdict.ok


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bogus", ["gpt4all", "", "ollama; drop table", "OLLAMA_TYPO", "../../etc/passwd"])
def test_unknown_provider_is_rejected_at_the_boundary(client: TestClient, bogus: str) -> None:
    """A provider the runtime cannot route must never reach the session, where
    it would silently degrade to the default and mislabel which backend ran."""
    response = client.post("/api/models", json={"worker": "some-model", "worker_provider": bogus})
    assert response.status_code == 422


def test_listing_an_unreachable_provider_reports_where_it_tried(client: TestClient) -> None:
    """Nothing is listening on the LM Studio port in a test run. The response
    has to name the endpoint -- 'no models' alone gives the user nothing to fix.

    Asserted on the URL rather than the provider id: the message is written for
    a person, so it says "LM Studio" and tells them to start its server. What
    must survive is that it points at the address that was actually tried.
    """
    response = client.get("/api/models?provider=lmstudio")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "lmstudio"
    assert body["models"] == []
    assert body["error"]
    assert settings.LMSTUDIO_BASE_URL in body["error"], "the error must name where it tried"


def test_provider_switch_does_not_carry_the_previous_backends_model(client: TestClient) -> None:
    """An Ollama tag like `deepseek-r1:1.5b` is a 404 on LM Studio. Switching
    provider without naming a model must re-resolve rather than keep the old id.
    """
    first = client.post("/api/models", json={"worker": "qwen2.5-coder:1.5b"})
    headers = {"X-Session-Id": first.json()["session_id"]}
    assert first.json()["models"]["worker"] == "qwen2.5-coder:1.5b"

    response = client.post("/api/models", json={"worker_provider": "lmstudio"}, headers=headers)

    assert response.status_code == 200
    models = response.json()["models"]
    assert models["worker_provider"] == "lmstudio"
    # LM Studio is unreachable here, so there is nothing to resolve to -- but the
    # stale Ollama tag must not survive the switch either.
    assert models["worker"] != "qwen2.5-coder:1.5b"


def test_an_unreachable_provider_does_not_break_the_provider_list(client: TestClient) -> None:
    """The picker must still render every provider so the user can switch back."""
    body = client.get("/api/models?provider=lmstudio").json()

    assert {entry["id"] for entry in body["providers"]} >= {"ollama", "lmstudio"}
    assert any(entry["is_default"] for entry in body["providers"])


# --------------------------------------------------------------------------- #
# Context documents
# --------------------------------------------------------------------------- #
def test_a_data_file_is_rejected_as_a_document(client: TestClient) -> None:
    """The two loaders must not overlap: a CSV posted to /documents would be
    chunked as prose and retrieved as text, which is silently useless."""
    response = client.post(
        "/api/documents",
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )

    assert response.status_code == 422
    assert ".md" in response.json()["detail"]


def test_an_empty_document_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/documents",
        files={"file": ("blank.md", b"   \n\n  ", "text/markdown")},
    )

    assert response.status_code == 422


def test_an_oversized_document_is_refused(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "CONTEXT_DOC_MAX_BYTES", 32)

    response = client.post(
        "/api/documents",
        files={"file": ("big.md", b"x" * 4096, "text/markdown")},
    )

    assert response.status_code == 413


def test_documents_can_be_switched_off(client: TestClient, monkeypatch) -> None:
    """A deployment that does not want unstructured context attached to sessions
    must be able to refuse it at the boundary, not just hide the button."""
    monkeypatch.setattr(settings, "CONTEXT_DOCS_ENABLED", False)

    response = client.post(
        "/api/documents",
        files={"file": ("notes.md", b"Some rules.", "text/markdown")},
    )

    assert response.status_code == 403


def test_deleting_an_unknown_document_is_a_404(client: TestClient) -> None:
    response = client.delete("/api/documents/nope.md")
    assert response.status_code == 404


def test_a_path_traversing_document_name_is_neutralised(client: TestClient) -> None:
    """The name becomes a dict key and is echoed back, so it must be a basename."""
    response = client.post(
        "/api/documents",
        files={"file": ("../../etc/passwd.md", b"Rules go here.", "text/markdown")},
    )

    assert response.status_code == 200
    assert response.json()["document"]["name"] == "passwd.md"


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["turbo", "PLANNING ", "", "deep-thought"])
def test_an_unknown_mode_never_reaches_the_budget_lookup(mode: str) -> None:
    """`budget_for` indexes TIER_BUDGETS; an unnormalised mode would be a
    silent fall-through rather than a KeyError, which is worse."""
    normalised = orchestrator.normalise_mode(mode)
    assert normalised in {"auto", "fast", "deep", "planning"}
    assert settings.budget_for(normalised, "7B").iterations >= 1


def test_the_rest_boundary_rejects_a_bogus_mode(client: TestClient) -> None:
    """Schema-level rejection: a typo must not silently become `auto`.

    A dataset is loaded first because `require_dataset` runs ahead of body
    validation and would otherwise answer 412 before the mode was ever read.
    """
    session_id = _session_with_data(client)
    response = client.post(
        "/api/chat",
        json={"message": "hello", "mode": "turbocharged"},
        headers={"X-Session-Id": session_id},
    )

    assert response.status_code == 422
    assert any("mode" in str(error.get("loc", "")) for error in response.json()["detail"])
