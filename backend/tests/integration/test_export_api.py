"""`GET /api/export/{message_id}` over the real HTTP app.

Distinct from `tests/unit/test_export.py`, which exercises the builders
directly: this checks the route's own contract -- session scoping, the 404s,
and zip-vs-bare-file branching -- through a real `TestClient` and real SQLite,
the same way `test_api.py` exercises the rest of the surface.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api.api import app
from src.core.analysis.runs import capture, dataset_manifest_from_session
from src.core.database import db_mgr
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


def seed_message(session_id: str, *, instruction: str = "how many rows?", steps: list[dict] | None = None) -> int:
    """Writes an assistant message the way `orchestrator._finalize` would,
    without running a real turn -- the route only reads `chat_messages.meta`."""
    default_steps = [{"goal": "count rows", "code": "print(len(df))"}]
    return db_mgr.append_chat_message(
        session_id,
        "assistant",
        "There are 5 rows.",
        {"code": "print(len(df))", "instruction": instruction, "steps": steps if steps is not None else default_steps},
    )


def test_a_file_based_session_exports_a_zip_with_its_data(client: TestClient, simple_df: pd.DataFrame) -> None:
    """A file-based table's data lives only in this session -- nowhere else --
    so a script that will actually run standalone next month has to carry a
    copy of it, not just the code. That is why even a single-table export
    zips rather than returning a bare `.py`."""
    session_id = upload(client, simple_df)["session_id"]
    message_id = seed_message(session_id)

    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: session_id})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    script = archive.read("analysis.py").decode("utf-8")
    assert "print(len(df))" in script
    assert "how many rows?" in script
    assert "data/data.csv" in archive.namelist()


def test_notebook_format_bundles_the_notebook_and_its_data(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    message_id = seed_message(session_id)

    response = client.get(
        f"/api/export/{message_id}", params={"format": "notebook"}, headers={SESSION_HEADER: session_id}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    notebook = json.loads(archive.read("analysis.ipynb"))
    assert notebook["nbformat"] == 4
    assert any("print(len(df))" in "".join(cell.get("source", [])) for cell in notebook["cells"])
    assert "data/data.csv" in archive.namelist()


def test_a_recorded_evidence_graph_is_bundled_as_provenance_json(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    message_id = seed_message(session_id)
    db_mgr.save_evidence_graph(
        session_id,
        message_id,
        {
            "nodes": [{"id": "execution-0", "kind": "execution", "label": "count rows", "data": {}, "at": 1.0}],
            "edges": [],
        },
    )

    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: session_id})

    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    provenance = json.loads(archive.read("provenance.json"))
    assert provenance["nodes"][0]["id"] == "execution-0"


def _capture_run(session_id: str, message_id: int, session, *, instruction: str = "how many rows?") -> None:
    run = capture(
        session_id=session_id,
        message_id=message_id,
        instruction=instruction,
        answer="There are 5 rows.",
        dataset_manifest=dataset_manifest_from_session(session),
        steps=[],
        analysis={"evidence": {"nodes": [], "edges": []}},
        warnings=[],
    )
    db_mgr.save_analysis_run(session_id, message_id, run.to_dict())


def test_a_captured_run_is_bundled_as_run_json(client: TestClient, simple_df: pd.DataFrame) -> None:
    """Phase 10 (ADR 0005): an immutable snapshot, once captured, travels with the export."""
    session_id = upload(client, simple_df)["session_id"]
    session = session_manager.get(session_id)
    assert session is not None
    message_id = seed_message(session_id)
    _capture_run(session_id, message_id, session)

    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: session_id})

    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    captured = json.loads(archive.read("run.json"))
    assert captured["instruction"] == "how many rows?"
    assert "DATASET_CHANGED.txt" not in archive.namelist()


def test_a_dataset_changed_since_capture_is_flagged_not_silently_reproduced(
    client: TestClient, simple_df: pd.DataFrame
) -> None:
    """The acceptance boundary this phase draws: dataset *bytes* are not versioned, only their
    content hash at capture time, so a later turn replacing the table must say so, not export
    silently as if nothing had changed underneath it."""
    session_id = upload(client, simple_df)["session_id"]
    session = session_manager.get(session_id)
    assert session is not None
    message_id = seed_message(session_id)
    _capture_run(session_id, message_id, session)

    session.add_dataset("data.csv", pd.DataFrame({"A": [999], "B": ["z"], "C": [9.9]}))

    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: session_id})

    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    warning = archive.read("DATASET_CHANGED.txt").decode("utf-8")
    assert "data.csv" in warning


def test_a_fully_connector_backed_session_exports_a_bare_file(client: TestClient, simple_df: pd.DataFrame) -> None:
    """Nothing to bundle when every table is connection-sourced -- the loader
    re-fetches by name instead, so the plain script is the whole export."""
    session_id = upload(client, simple_df)["session_id"]
    session = session_manager.get(session_id)
    assert session is not None
    handle = session.active_handle
    assert handle is not None
    handle.origin = "Shop"
    handle.profile["target"] = "orders"
    message_id = seed_message(session_id)

    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: session_id})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-python")
    assert "connection_store.by_name('Shop')" in response.text


def test_multiple_tables_produce_a_zip_bundle(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    session = session_manager.get(session_id)
    assert session is not None
    session.add_dataset("extra.csv", pd.DataFrame({"x": [1, 2]}), make_active=False)
    message_id = seed_message(session_id)

    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: session_id})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert "analysis.py" in names
    assert "data/data.csv" in names
    assert "data/extra.csv" in names


def test_unknown_message_id_is_404(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]

    response = client.get("/api/export/999999", headers={SESSION_HEADER: session_id})

    assert response.status_code == 404


def test_a_message_with_no_recorded_steps_is_404(client: TestClient, simple_df: pd.DataFrame) -> None:
    session_id = upload(client, simple_df)["session_id"]
    message_id = seed_message(session_id, steps=[])

    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: session_id})

    assert response.status_code == 404


def test_a_message_from_another_session_is_not_reachable(client: TestClient, simple_df: pd.DataFrame) -> None:
    """The route's only access check: a message id from a different session
    must not be exportable through this session's header."""
    owner = upload(client, simple_df)["session_id"]
    message_id = seed_message(owner)

    other = client.post("/api/session").json()["session_id"]
    response = client.get(f"/api/export/{message_id}", headers={SESSION_HEADER: other})

    assert response.status_code == 404


def test_an_unknown_session_is_404(client: TestClient) -> None:
    response = client.get("/api/export/1", headers={SESSION_HEADER: "does-not-exist"})
    assert response.status_code == 404
