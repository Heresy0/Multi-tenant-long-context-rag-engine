from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from backend.app.api.system import router
from backend.app.db.base import Base
from backend.app.db.session import create_session_factory
from backend.app.metrics import (
    REGISTRY,
    observe_http_request,
)
from backend.app.worker_heartbeat_service import (
    WorkerHeartbeatService,
)


@pytest.fixture
def metrics_app() -> Iterator[tuple[FastAPI, object]]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    app = FastAPI()
    app.state.database_engine = engine
    app.state.database_session_factory = session_factory
    app.state.settings = SimpleNamespace(
        indexing_worker_stale_seconds=30,
    )
    app.middleware("http")(observe_http_request)
    app.include_router(router)

    @app.get("/metric-items/{item_id}")
    def metric_item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    try:
        yield app, session_factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_metrics_endpoint_exports_queue_and_worker_gauges(
    metrics_app,
) -> None:
    app, session_factory = metrics_app

    with session_factory() as session:
        WorkerHeartbeatService(
            session=session
        ).register(worker_id="metrics-worker")

    response = TestClient(app).get("/metrics")
    body = response.text

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "text/plain"
    )
    assert "enterprise_indexing_workers_active 1.0" in body
    assert (
        'enterprise_indexing_queue_jobs{status="queued"} 0.0'
        in body
    )


def test_http_metrics_use_route_template_not_identifier(
    metrics_app,
) -> None:
    app, _ = metrics_app
    client = TestClient(app)
    labels = {
        "method": "GET",
        "route": "/metric-items/{item_id}",
        "status": "200",
    }
    before = (
        REGISTRY.get_sample_value(
            "enterprise_http_requests_total",
            labels,
        )
        or 0
    )
    secret_identifier = f"secret-{uuid4()}"

    response = client.get(
        f"/metric-items/{secret_identifier}"
    )
    metrics = client.get("/metrics").text

    assert response.status_code == 200
    assert REGISTRY.get_sample_value(
        "enterprise_http_requests_total",
        labels,
    ) == before + 1
    assert secret_identifier not in metrics


def test_metrics_do_not_export_business_identifiers(
    metrics_app,
) -> None:
    app, _ = metrics_app
    metrics = TestClient(app).get("/metrics").text

    assert "tenant_id" not in metrics
    assert "user_id" not in metrics
    assert "document_id" not in metrics
    assert "job_id" not in metrics
    assert "file_name" not in metrics
