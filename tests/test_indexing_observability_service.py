from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import DocumentIndexingJob
from backend.app.indexing_observability_service import (
    IndexingObservabilityService,
)
from backend.app.worker_heartbeat_service import (
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


def _job(
    *,
    status: str,
    created_at: datetime,
    finished_at: datetime | None = None,
) -> DocumentIndexingJob:
    marker = uuid4().hex
    return DocumentIndexingJob(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        document_id=uuid4(),
        requested_by_user_id=uuid4(),
        staged_storage_uri=f"file:///staging/{marker}.txt",
        candidate_file_name=f"{marker}.txt",
        candidate_mime_type="text/plain",
        candidate_content_hash="a" * 64,
        target_version=1,
        status=status,
        attempt_count=0,
        max_attempts=3,
        available_at=created_at,
        started_at=(
            created_at
            if status == "running"
            else None
        ),
        finished_at=finished_at,
        created_at=created_at,
    )


def test_builds_worker_and_queue_snapshot(
    session: Session,
    monkeypatch,
) -> None:
    now = datetime(
        2026, 9, 30, 12, 0, tzinfo=timezone.utc
    )
    monkeypatch.setattr(
        WorkerHeartbeatService,
        "_now",
        staticmethod(lambda: now),
    )
    monkeypatch.setattr(
        IndexingObservabilityService,
        "_now",
        staticmethod(lambda: now),
    )
    WorkerHeartbeatService(session=session).register(
        worker_id="worker-a"
    )
    session.add_all([
        _job(
            status="queued",
            created_at=now - timedelta(minutes=5),
        ),
        _job(
            status="running",
            created_at=now - timedelta(minutes=1),
        ),
        _job(
            status="succeeded",
            created_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=30),
        ),
        _job(
            status="failed",
            created_at=now - timedelta(days=2),
            finished_at=now - timedelta(days=2),
        ),
    ])
    session.commit()

    snapshot = IndexingObservabilityService(
        session=session
    ).snapshot(worker_stale_after_seconds=30)

    assert snapshot.status == "ok"
    assert snapshot.active_workers == 1
    assert snapshot.last_heartbeat_at == now
    assert snapshot.queue.queued == 1
    assert snapshot.queue.running == 1
    assert snapshot.queue.succeeded == 1
    assert snapshot.queue.failed == 1
    assert snapshot.queue.oldest_queued_seconds == 300
    assert snapshot.queue.succeeded_last_24_hours == 1
    assert snapshot.queue.failed_last_24_hours == 0


def test_reports_unavailable_without_fresh_worker(
    session: Session,
) -> None:
    snapshot = IndexingObservabilityService(
        session=session
    ).snapshot(worker_stale_after_seconds=30)

    assert snapshot.status == "unavailable"
    assert snapshot.active_workers == 0
    assert snapshot.last_heartbeat_at is None
