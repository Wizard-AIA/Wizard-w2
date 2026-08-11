"""Integration tests over the HTTP surface.

These exercise the real FastAPI app with a real SessionManager and real SQLite.
Only the LLM is stubbed — everything else runs. Docker is disabled through the
test environment, so execution takes the local fallback path.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api.api import app
from src.core.session import session_manager


SESSION_HEADER = "X-Session-Id"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    session_manager.shutdown()


def csv_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8")


def upload(client: TestClient, df: pd.DataFrame, name: str = "data.csv") -> dict:
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": (name, csv_bytes(df), "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Health and capability discovery
# --------------------------------------------------------------------------- #
def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_config_advertises_capabilities(client: TestClient) -> None:
    payload = client.get("/api/config").json()
    formats = {entry.lstrip(".") for entry in payload["supported_formats"]}

    assert {"csv", "parquet", "json", "xlsx", "feather"} <= formats
    assert payload["sandbox_enabled"] is False  # disabled for tests
    assert payload["queue_backend"] == "in-process"
    assert payload["requires_api_key"] is False


def test_openapi_document_is_generated(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/chat" in schema["paths"]
    assert "/api/datasets" in schema["paths"]


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def test_session_is_created_and_echoed_in_a_header(client: TestClient) -> None:
    response = client.post("/api/session")
    assert response.status_code == 200
    assert response.headers[SESSION_HEADER]
    assert response.json()["has_data"] is False


def test_sessions_are_isolated_from_each_other(client: TestClient) -> None:
    """Regression: a single module-level `state` dict meant two browsers shared
    one dataset, and whoever uploaded last won."""
    first = client.post("/api/session").json()["session_id"]
    second = client.post("/api/session").json()["session_id"]
    assert first != second

    client.post(
        "/api/datasets?clean=false",
        files={"file": ("a.csv", csv_bytes(pd.DataFrame({"a": [1]})), "text/csv")},
        headers={SESSION_HEADER: first},
    )

    owner = client.get("/api/session", headers={SESSION_HEADER: first}).json()
    other = client.get("/api/session", headers={SESSION_HEADER: second}).json()

    assert owner["has_data"] is True
    assert other["has_data"] is False, "a dataset leaked across sessions"


def test_unknown_session_id_is_rejected_not_silently_replaced(client: TestClient) -> None:
    """Regression (issue #94): an invalid/expired/forged X-Session-Id must not
    silently mint a fresh unauthenticated session -- that would detach the
    caller from whatever workspace/data-mode/policy state it thought it had."""
    response = client.get("/api/session", headers={SESSION_HEADER: "does-not-exist"})
    assert response.status_code == 404


def test_omitted_session_id_yields_a_fresh_session(client: TestClient) -> None:
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json()["session_id"]


def test_session_delete(client: TestClient) -> None:
    session_id = client.post("/api/session").json()["session_id"]
    response = client.delete("/api/session", headers={SESSION_HEADER: session_id})
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
def test_upload_csv(client: TestClient, simple_df: pd.DataFrame) -> None:
    payload = upload(client, simple_df)
    assert payload["dataset"]["rows"] == 5
    assert payload["dataset"]["columns"] == ["A", "B", "C"]
    assert payload["session_id"]


def test_upload_parquet(client: TestClient, simple_df: pd.DataFrame, tmp_path) -> None:
    path = tmp_path / "data.parquet"
    simple_df.to_parquet(path)

    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("data.parquet", path.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 200
    assert response.json()["dataset"]["source_format"] == "parquet"


def test_upload_json(client: TestClient) -> None:
    payload = json.dumps([{"a": 1, "b": 2}, {"a": 3, "b": 4}]).encode()
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("data.json", payload, "application/json")},
    )
    assert response.status_code == 200
    assert response.json()["dataset"]["rows"] == 2


def test_upload_rejects_unsupported_type(client: TestClient) -> None:
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("payload.exe", b"MZ\x00\x00", "application/octet-stream")},
    )
    assert response.status_code == 422
    assert "Supported formats" in response.json()["detail"]


def test_upload_rejects_header_only_csv(client: TestClient) -> None:
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("empty.csv", b"a,b,c\n", "text/csv")},
    )
    assert response.status_code == 400


def test_upload_normalises_colliding_column_names(client: TestClient) -> None:
    body = b"a-b,a.b,a b\n1,2,3\n"
    response = client.post("/api/datasets?clean=false", files={"file": ("c.csv", body, "text/csv")})

    assert response.status_code == 200
    columns = response.json()["dataset"]["columns"]
    assert len(columns) == len(set(columns)), f"duplicate columns produced: {columns}"


def test_multiple_datasets_can_coexist_in_a_session(client: TestClient) -> None:
    session_id = client.post("/api/session").json()["session_id"]
    headers = {SESSION_HEADER: session_id}

    for name in ("first.csv", "second.csv"):
        client.post(
            "/api/datasets?clean=false",
            files={"file": (name, csv_bytes(pd.DataFrame({"x": [1, 2]})), "text/csv")},
            headers=headers,
        )

    payload = client.get("/api/datasets", headers=headers).json()
    assert {dataset["name"] for dataset in payload["datasets"]} == {"first.csv", "second.csv"}
    assert payload["active_dataset"] == "second.csv"

    activated = client.post("/api/datasets/first.csv/activate", headers=headers).json()
    assert activated["active_dataset"] == "first.csv"


def test_activating_an_unknown_dataset_is_404(client: TestClient) -> None:
    response = client.post("/api/datasets/nope.csv/activate")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def test_preview_requires_a_dataset(client: TestClient) -> None:
    response = client.get("/api/data/preview")
    assert response.status_code == 412


def test_preview_paginates(client: TestClient) -> None:
    session_id = client.post("/api/session").json()["session_id"]
    headers = {SESSION_HEADER: session_id}
    client.post(
        "/api/datasets?clean=false",
        files={"file": ("d.csv", csv_bytes(pd.DataFrame({"n": range(120)})), "text/csv")},
        headers=headers,
    )

    payload = client.get("/api/data/preview?page=2&per_page=50", headers=headers).json()
    assert payload["page"] == 2
    assert len(payload["data"]) == 50
    assert payload["total_rows"] == 120
    assert payload["total_pages"] == 3


def test_preview_sorts(client: TestClient) -> None:
    session_id = client.post("/api/session").json()["session_id"]
    headers = {SESSION_HEADER: session_id}
    client.post(
        "/api/datasets?clean=false",
        files={"file": ("d.csv", csv_bytes(pd.DataFrame({"n": [3, 1, 2]})), "text/csv")},
        headers=headers,
    )

    payload = client.get("/api/data/preview?sort_by=n&sort_order=desc", headers=headers).json()
    assert [row["n"] for row in payload["data"]] == [3, 2, 1]


def test_preview_rejects_an_unknown_sort_column(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    response = client.get("/api/data/preview?sort_by=nope", headers={SESSION_HEADER: session_id})
    assert response.status_code == 400


def test_preview_serialises_non_finite_values(client: TestClient) -> None:
    """Regression: NaN/Inf are invalid JSON and previously produced a 500."""
    body = b"a,b\n1,2\n,3\n"
    session_id = client.post("/api/datasets?clean=false", files={"file": ("d.csv", body, "text/csv")}).json()[
        "session_id"
    ]

    response = client.get("/api/data/preview", headers={SESSION_HEADER: session_id})
    assert response.status_code == 200
    json.dumps(response.json())  # must round-trip


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #
def test_workspace_listing_is_session_scoped(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    payload = client.get("/api/workspace/files", headers={SESSION_HEADER: session_id}).json()
    assert any(entry["name"] == "dataset.csv" for entry in payload["files"])


@pytest.mark.parametrize(
    "path",
    ["../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd"],
)
def test_workspace_rejects_path_traversal(client: TestClient, path: str) -> None:
    response = client.get(f"/api/workspace/file/{path}")
    assert response.status_code in (403, 404), response.text


def test_workspace_file_download(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    response = client.get("/api/workspace/file/dataset.csv", headers={SESSION_HEADER: session_id})
    assert response.status_code == 200
    assert "A,B,C" in response.text


def test_protected_dataset_cannot_be_deleted(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    response = client.delete("/api/workspace/file/dataset.csv", headers={SESSION_HEADER: session_id})
    assert response.status_code == 400


def test_plot_html_served_inline_and_sandboxed(client: TestClient, simple_df: pd.DataFrame) -> None:
    """Plots must render inline in the frontend's sandboxed iframe, and stay

    confined server-side too: `attachment` (the FileResponse default) makes
    browsers refuse to display an HTML chart inside a sandboxed iframe at
    all, and a bare CSP is a second enforcement of the iframe's own sandbox
    restrictions for anyone who opens the URL directly.
    """
    session_id = upload(client, simple_df)["session_id"]
    session = session_manager.get(session_id)
    assert session is not None
    (session.workspace / "plot.html").write_text("<html><body>chart</body></html>", encoding="utf-8")

    response = client.get("/api/workspace/file/plot.html", headers={SESSION_HEADER: session_id})

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["content-security-policy"] == "sandbox allow-scripts"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_dataset_download_stays_an_attachment(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    response = client.get("/api/workspace/file/dataset.csv", headers={SESSION_HEADER: session_id})
    assert response.headers["content-disposition"].startswith("attachment")
    assert "content-security-policy" not in response.headers


# --------------------------------------------------------------------------- #
# Sandbox routes (degraded, since Docker is disabled here)
# --------------------------------------------------------------------------- #
def test_variables_endpoint_reports_no_sandbox(client: TestClient) -> None:
    payload = client.get("/api/sandbox/variables").json()
    assert payload["sandbox_available"] is False
    assert payload["variables"] == {}


def test_interrupt_is_safe_without_a_sandbox(client: TestClient) -> None:
    response = client.post("/api/sandbox/interrupt")
    assert response.status_code == 200


@pytest.mark.parametrize("name", ["__builtins__", "a b", "x'; import os; y='"])
def test_variable_export_rejects_unsafe_names(client: TestClient, name: str) -> None:
    """The name is interpolated into generated source, so it must be an identifier."""
    response = client.post(f"/api/sandbox/variables/{name}/export")
    assert response.status_code in (400, 404)


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/api/jobs/nonexistent").status_code == 404


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def test_report_is_generated_even_with_no_history(client: TestClient) -> None:
    """Regression: this raised AttributeError and returned 500 on every call,
    because ReportingEngine read a `working_memory.memories` list attribute that
    the SQLite migration had removed."""
    response = client.get("/api/report")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["report"], str)
    assert payload["interaction_count"] == 0


# --------------------------------------------------------------------------- #
# Chat validation
# --------------------------------------------------------------------------- #
def test_chat_requires_a_dataset(client: TestClient) -> None:
    response = client.post("/api/chat", json={"message": "hello"})
    assert response.status_code == 412


def test_chat_rejects_an_empty_message(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    response = client.post("/api/chat", json={"message": ""}, headers={SESSION_HEADER: session_id})
    assert response.status_code == 422


def test_chat_rejects_an_oversized_message(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    response = client.post("/api/chat", json={"message": "x" * 20_000}, headers={SESSION_HEADER: session_id})
    assert response.status_code == 422


def test_chat_rejects_an_invalid_mode(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    response = client.post("/api/chat", json={"message": "hi", "mode": "turbo"}, headers={SESSION_HEADER: session_id})
    assert response.status_code == 422


def test_model_selection_round_trips_provider_per_role(client: TestClient) -> None:
    """The session has to remember *which backend* each role runs on, not just
    the model name -- the same name can exist on two providers."""
    response = client.post(
        "/api/models",
        json={
            "manager": "deepseek-r1:1.5b",
            "manager_provider": "ollama",
            "worker": "qwen2.5-coder-7b-instruct",
            "worker_provider": "lmstudio",
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]

    models = client.get("/api/models", headers={SESSION_HEADER: session_id}).json()["selected"]
    assert models["manager_provider"] == "ollama"
    assert models["worker_provider"] == "lmstudio"
    assert models["worker"] == "qwen2.5-coder-7b-instruct"


def test_model_list_advertises_every_provider(client: TestClient) -> None:
    body = client.get("/api/models").json()

    listed = {entry["id"]: entry for entry in body["providers"]}
    assert {"ollama", "lmstudio", "openai", "custom_gateway"} <= listed.keys()
    assert listed["lmstudio"]["local"] is True
    assert listed["lmstudio"]["base_url"]
    assert listed["custom_gateway"]["configured"] is False


def test_partial_selection_leaves_other_roles_alone(client: TestClient) -> None:
    first = client.post("/api/models", json={"manager": "keep-me", "manager_provider": "ollama"})
    headers = {SESSION_HEADER: first.json()["session_id"]}

    client.post("/api/models", json={"temperature": 0.4}, headers=headers)

    models = client.get("/api/models", headers=headers).json()["selected"]
    assert models["manager"] == "keep-me"
    assert models["manager_provider"] == "ollama"
    assert models["temperature"] == 0.4


# --------------------------------------------------------------------------- #
# Context documents
# --------------------------------------------------------------------------- #
def test_a_document_can_be_attached_listed_and_removed(client: TestClient) -> None:
    """The full lifecycle the /data page drives."""
    upload = client.post(
        "/api/documents",
        files={"file": ("dictionary.md", b"# Dictionary\n\n`status` C means cancelled.\n", "text/markdown")},
    )
    assert upload.status_code == 200

    document = upload.json()["document"]
    assert document["name"] == "dictionary.md"
    assert document["chunks"] >= 1
    assert document["chars"] > 0

    session_id = upload.json()["session_id"]
    listed = client.get("/api/datasets", headers={"X-Session-Id": session_id}).json()
    assert [entry["name"] for entry in listed["documents"]] == ["dictionary.md"]

    removed = client.delete("/api/documents/dictionary.md", headers={"X-Session-Id": session_id})
    assert removed.status_code == 200
    assert client.get("/api/datasets", headers={"X-Session-Id": session_id}).json()["documents"] == []


def test_documents_are_scoped_to_their_session(client: TestClient) -> None:
    """Reference documents carry business rules; leaking them across sessions
    would be the same class of defect as the shared dataset this app already
    fixed once."""
    first = client.post(
        "/api/documents",
        files={"file": ("secret.md", b"Internal margin formula.", "text/markdown")},
    ).json()["session_id"]

    other = client.post("/api/session").json()["session_id"]
    assert other != first

    listing = client.get("/api/datasets", headers={"X-Session-Id": other}).json()
    assert listing["documents"] == []


# --------------------------------------------------------------------------- #
# Config surface the client renders from
# --------------------------------------------------------------------------- #
def test_config_reports_the_agent_settings(client: TestClient) -> None:
    """The settings page renders these directly; a missing field is a blank row."""
    config = client.get("/api/config").json()

    assert config["agent_tier"] in {"auto", "compact", "balanced", "full"}
    assert config["agent_max_iterations"] >= 1
    assert isinstance(config["agent_require_approval"], bool)
    assert isinstance(config["agent_verify"], bool)
    assert isinstance(config["agent_grounding_check"], bool)
    assert isinstance(config["context_docs_enabled"], bool)
    assert ".md" in config["supported_document_formats"]


def test_only_genuinely_ambiguous_formats_are_claimed_by_both_loaders(client: TestClient) -> None:
    """A `.txt` really can be either a tab-delimited export or a data dictionary,
    and the endpoint the user posts to decides which. That is the *only*
    acceptable overlap: a structured format appearing in the document list, or a
    prose format in the data list, means one of them will be parsed the wrong way.
    """
    config = client.get("/api/config").json()

    data = {f".{extension.lstrip('.')}" for extension in config["supported_formats"]}
    documents = set(config["supported_document_formats"])

    assert data & documents == {".txt"}, f"unexpected overlap: {(data & documents) - {'.txt'}}"
    # The formats that carry structure must belong to exactly one loader.
    assert {".csv", ".parquet", ".xlsx", ".feather"} <= data
    assert not ({".csv", ".parquet", ".xlsx", ".feather"} & documents)
    assert {".md", ".pdf", ".docx"} <= documents
    assert not ({".md", ".pdf", ".docx"} & data)


def test_a_dataset_summary_exposes_its_table_key(client: TestClient) -> None:
    """Generated code addresses tables by key, and /data shows the user which."""
    response = client.post(
        "/api/datasets?clean=false",
        files={"file": ("Q3 orders (final).csv", b"a,b\n1,2\n", "text/csv")},
    )

    assert response.status_code == 200
    assert response.json()["dataset"]["table_key"] == "q3_orders_final"


def test_chat_response_carries_the_investigation_fields(client: TestClient, monkeypatch) -> None:
    """The REST path is used by scripts that have no event stream, so everything
    the WebSocket emits as frames has to also be present on the final object."""
    from stubs import ScriptedLLM

    monkeypatch.setattr(
        "src.core.agent.orchestrator.llm_provider",
        ScriptedLLM(["1. Count", "```python\nprint(len(df))\n```", "There is 1 row."]),
    )
    session_id = client.post("/api/datasets?clean=false", files={"file": ("d.csv", b"a\n1\n", "text/csv")}).json()[
        "session_id"
    ]

    body = client.post(
        "/api/chat",
        json={"message": "how many rows", "mode": "fast"},
        headers={"X-Session-Id": session_id},
    ).json()

    for field in ("findings", "assumptions", "iterations", "tier", "mode", "verification", "grounding"):
        assert field in body, f"missing {field}"
    assert body["mode"] == "fast"
    assert body["iterations"] >= 1
