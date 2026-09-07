"""HTTP metrics must reflect actual API dispatch without leaking request IDs."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.infra.metrics import MetricsCollector


def test_request_metrics_use_route_template_and_status(monkeypatch) -> None:
    import src.api.api as api_module

    collector = MetricsCollector()
    monkeypatch.setattr(api_module, "metrics", collector)
    app = FastAPI()
    app.middleware("http")(api_module.record_http_metrics)

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"id": item_id}

    response = TestClient(app).get("/items/customer-secret-42")

    assert response.status_code == 200
    rendered = collector.generate_prometheus_text()
    assert 'endpoint="/items/{item_id}"' in rendered
    assert "customer-secret-42" not in rendered
    assert 'status="200"' in rendered
