"""Pins for security-audit batch 3 (#95, #96, #99, #100, #91).

Each of the other fixes has its own home (`test_sandbox_policy.py` for pip
package validation, `test_documents.py` for PDF bounds, `test_model_downloader.py`
for Hugging Face URL validation). This file covers the two that are only
observable at the route layer: the model-deletion confirmation and skill
routes now requiring a resolvable session.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.api.api import RATE_LIMITED_PREFIXES, app
from src.core.llm.downloader import is_valid_model_name
from src.core.session import session_manager


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    session_manager.shutdown()


# --------------------------------------------------------------------------- #
# #100 -- model deletion needs an explicit confirmation and is rate-limited
# --------------------------------------------------------------------------- #
def test_deleting_a_model_without_confirmation_is_refused(client: TestClient) -> None:
    response = client.delete("/api/models/installed", params={"model": "llama3"})
    assert response.status_code == 400
    assert "confirm" in response.json()["detail"].lower()


def test_deleting_a_model_with_a_mismatched_confirmation_is_refused(client: TestClient) -> None:
    response = client.delete("/api/models/installed", params={"model": "llama3", "confirm": "not-llama3"})
    assert response.status_code == 400


def test_model_routes_are_in_the_rate_limited_set() -> None:
    """`POST/DELETE /api/models/*` must be covered so a scripted delete or
    download loop cannot be fired without bound."""
    assert "/api/models" in RATE_LIMITED_PREFIXES


# --------------------------------------------------------------------------- #
# #91 -- skill discovery and candidate routes require a resolvable session
# --------------------------------------------------------------------------- #
def test_skill_discovery_rejects_an_unresolvable_session(client: TestClient) -> None:
    response = client.get("/api/skills", headers={"X-Session-Id": "no-such-session"})
    assert response.status_code == 404


def test_skill_candidates_rejects_an_unresolvable_session(client: TestClient) -> None:
    response = client.get("/api/skills/candidates", headers={"X-Session-Id": "no-such-session"})
    assert response.status_code == 404


def test_getting_one_skill_rejects_an_unresolvable_session(client: TestClient) -> None:
    response = client.get("/api/skills/some-skill", headers={"X-Session-Id": "no-such-session"})
    assert response.status_code == 404


def test_skill_discovery_still_works_with_no_session_header(client: TestClient) -> None:
    """No header means "create one for me", same as every other route -- only
    a session id that was sent and doesn't resolve is rejected.
    """
    response = client.get("/api/skills")
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# #99 -- Hugging Face URL validation, defense-in-depth cases beyond the regex
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "https://user@huggingface.co/org/repo",
        "https://user:pass@huggingface.co/org/repo",
        "https://huggingface.co/org/repo?ref=evil",
        "https://huggingface.co/org/repo#fragment",
    ],
)
def test_huggingface_urls_with_userinfo_or_query_are_rejected(url: str) -> None:
    assert not is_valid_model_name(url)
