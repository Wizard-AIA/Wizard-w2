"""Production health must not claim an unconfined runtime is deployable."""

from __future__ import annotations

import pytest

from src.api.routes import meta


async def test_production_health_requires_docker_confinement(monkeypatch) -> None:
    monkeypatch.setattr(meta.settings, "ENV", "prod")
    monkeypatch.setattr(meta.runtime_backend, "active_backend", lambda: "host")

    response = await meta.health_ready()

    assert response.status_code == 503
    assert b"docker backend required" in response.body


async def test_development_host_runtime_is_explicitly_degraded_not_mislabeled(monkeypatch) -> None:
    monkeypatch.setattr(meta.settings, "ENV", "dev")
    monkeypatch.setattr(meta.runtime_backend, "active_backend", lambda: "host")

    response = await meta.health_ready()

    assert response.status_code == 200
    assert b"development host subprocess" in response.body
