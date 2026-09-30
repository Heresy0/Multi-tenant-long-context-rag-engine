from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import (
    DocumentIndexingJob,
    IndexingWorkerHeartbeat,
)
from .worker_heartbeat_service import (
    WorkerHeartbeatService,
)


@dataclass(frozen=True, slots=True)
class IndexingQueueSnapshot:
    queued: int
    running: int
    succeeded: int
    failed: int
    oldest_queued_seconds: float | None
    succeeded_last_24_hours: int
    failed_last_24_hours: int


@dataclass(frozen=True, slots=True)
class IndexingHealthSnapshot:
    status: str
    active_workers: int
    last_heartbeat_at: datetime | None
    queue: IndexingQueueSnapshot


class IndexingObservabilityService:
    """汇总 Worker 存活状态和索引任务队列指标。"""

    def __init__(self, *, session: Session) -> None:
        self._session = session

    def snapshot(
        self,
        *,
        worker_stale_after_seconds: float,
    ) -> IndexingHealthSnapshot:
        if worker_stale_after_seconds <= 0:
            raise ValueError(
                "worker_stale_after_seconds 必须大于 0"
            )

        now = self._now()
        active_workers = WorkerHeartbeatService(
            session=self._session
        ).list_active(
            stale_after_seconds=(
                worker_stale_after_seconds
            )
        )
        last_heartbeat_at = self._session.scalar(
            select(
                func.max(
                    IndexingWorkerHeartbeat
                    .last_heartbeat_at
                )
            )
        )
        status_rows = self._session.execute(
            select(
                DocumentIndexingJob.status,
                func.count(DocumentIndexingJob.id),
            ).group_by(DocumentIndexingJob.status)
        ).all()
        counts = {
            status: int(count)
            for status, count in status_rows
        }
        oldest_queued_at = self._session.scalar(
            select(func.min(DocumentIndexingJob.created_at))
            .where(DocumentIndexingJob.status == "queued")
        )
        oldest_queued_seconds = None

        if oldest_queued_at is not None:
            oldest_queued_seconds = max(
                (
                    now
                    - self._as_utc(oldest_queued_at)
                ).total_seconds(),
                0.0,
            )

        recent_cutoff = now - timedelta(hours=24)
        recent_rows = self._session.execute(
            select(
                DocumentIndexingJob.status,
                func.count(DocumentIndexingJob.id),
            )
            .where(
                DocumentIndexingJob.status.in_(
                    ("succeeded", "failed")
                ),
                DocumentIndexingJob.finished_at
                >= recent_cutoff,
            )
            .group_by(DocumentIndexingJob.status)
        ).all()
        recent_counts = {
            status: int(count)
            for status, count in recent_rows
        }

        return IndexingHealthSnapshot(
            status=(
                "ok"
                if active_workers
                else "unavailable"
            ),
            active_workers=len(active_workers),
            last_heartbeat_at=(
                self._as_utc(last_heartbeat_at)
                if last_heartbeat_at is not None
                else None
            ),
            queue=IndexingQueueSnapshot(
                queued=counts.get("queued", 0),
                running=counts.get("running", 0),
                succeeded=counts.get("succeeded", 0),
                failed=counts.get("failed", 0),
                oldest_queued_seconds=(
                    oldest_queued_seconds
                ),
                succeeded_last_24_hours=(
                    recent_counts.get("succeeded", 0)
                ),
                failed_last_24_hours=(
                    recent_counts.get("failed", 0)
                ),
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
