from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.indexing.worker_heartbeat_service import (
    WorkerHeartbeatService,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as database_session:
        yield database_session

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_registers_heartbeats_and_marks_worker_stopped(
    session: Session,
    monkeypatch,
) -> None:
    started_at = datetime(
        2026, 9, 30, 12, 0, tzinfo=timezone.utc
    )
    current = {"value": started_at}
    monkeypatch.setattr(
        WorkerHeartbeatService,
        "_now",
        staticmethod(lambda: current["value"]),
    )
    service = WorkerHeartbeatService(session=session)

    registered = service.register(worker_id="worker-a")
    assert registered.status == "running"
    assert registered.started_at.replace(
        tzinfo=timezone.utc
    ) == started_at

    current["value"] = started_at + timedelta(seconds=10)
    updated = service.heartbeat(worker_id="worker-a")
    assert updated.last_heartbeat_at.replace(
        tzinfo=timezone.utc
    ) == current["value"]
    assert [
        worker.worker_id
        for worker in service.list_active(
            stale_after_seconds=30
        )
    ] == ["worker-a"]

    stopped = service.mark_stopped(
        worker_id="worker-a"
    )
    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.stopped_at is not None
    assert stopped.stopped_at.replace(
        tzinfo=timezone.utc
    ) == current["value"]
    assert service.list_active(
        stale_after_seconds=30
    ) == []


def test_excludes_stale_worker_and_supports_multiple_instances(
    session: Session,
    monkeypatch,
) -> None:
    started_at = datetime(
        2026, 9, 30, 12, 0, tzinfo=timezone.utc
    )
    current = {"value": started_at}
    monkeypatch.setattr(
        WorkerHeartbeatService,
        "_now",
        staticmethod(lambda: current["value"]),
    )
    service = WorkerHeartbeatService(session=session)
    service.register(worker_id="worker-a")

    current["value"] = started_at + timedelta(seconds=20)
    service.register(worker_id="worker-b")
    current["value"] = started_at + timedelta(seconds=35)

    active = service.list_active(stale_after_seconds=30)

    assert [worker.worker_id for worker in active] == [
        "worker-b",
    ]


def test_rejects_invalid_worker_identifier(
    session: Session,
) -> None:
    service = WorkerHeartbeatService(session=session)

    with pytest.raises(ValueError, match="不能为空"):
        service.register(worker_id="  ")

    with pytest.raises(ValueError, match="255"):
        service.register(worker_id="x" * 256)
