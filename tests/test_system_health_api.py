from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

from backend.app.api.system import router
from backend.app.db.base import Base
from backend.app.db.session import create_session_factory
from backend.app.worker_heartbeat_service import (
    WorkerHeartbeatService,
)


class HealthyRedis:
    def ping(self) -> bool:
        return True


class BrokenRedis:
    def ping(self) -> bool:
        raise RuntimeError("redis unavailable")


@pytest.fixture
def health_app(tmp_path: Path):
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    app = FastAPI()
    app.state.redis_client = HealthyRedis()
    app.state.database_engine = engine
    app.state.database_session_factory = session_factory
    app.state.settings = SimpleNamespace(
        indexing_worker_stale_seconds=30,
    )
    app.include_router(router)

    try:
        yield app, engine, session_factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_liveness_and_readiness_are_separate(
    health_app,
) -> None:
    app, _, _ = health_app
    client = TestClient(app)

    assert client.get("/health").json() == {
        "status": "ok"
    }
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ok",
        "database": "up",
        "redis": "up",
    }


def test_indexing_health_requires_fresh_worker(
    health_app,
) -> None:
    app, _, session_factory = health_app
    client = TestClient(app)

    unavailable = client.get("/health/indexing")
    assert unavailable.status_code == 503
    assert unavailable.json()["active_workers"] == 0

    with session_factory() as session:
        WorkerHeartbeatService(
            session=session
        ).register(worker_id="worker-a")

    healthy = client.get("/health/indexing")
    assert healthy.status_code == 200
    assert healthy.json()["status"] == "ok"
    assert healthy.json()["active_workers"] == 1


def test_readiness_returns_503_when_database_is_down(
    health_app,
) -> None:
    app, _, _ = health_app

    class BrokenEngine:
        def connect(self):
            raise RuntimeError("database unavailable")

    app.state.database_engine = BrokenEngine()
    response = TestClient(app).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "down",
        "redis": "up",
    }


def test_readiness_returns_503_when_redis_is_down(
    health_app,
) -> None:
    app, _, _ = health_app
    app.state.redis_client = BrokenRedis()

    response = TestClient(app).get(
        "/health/ready"
    )

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "up",
        "redis": "down",
    }
