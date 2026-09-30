from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import IndexingWorkerHeartbeat


class WorkerHeartbeatService:
    """注册 Worker，并维护可跨进程查询的心跳状态。"""

    def __init__(self, *, session: Session) -> None:
        self._session = session

    def register(
        self,
        *,
        worker_id: str,
    ) -> IndexingWorkerHeartbeat:
        normalized_id = self._validate_worker_id(worker_id)
        now = self._now()

        try:
            heartbeat = self._session.get(
                IndexingWorkerHeartbeat,
                normalized_id,
            )

            if heartbeat is None:
                heartbeat = IndexingWorkerHeartbeat(
                    worker_id=normalized_id,
                    status="running",
                    started_at=now,
                    last_heartbeat_at=now,
                )
                self._session.add(heartbeat)
            else:
                heartbeat.status = "running"
                heartbeat.started_at = now
                heartbeat.last_heartbeat_at = now
                heartbeat.stopped_at = None
                heartbeat.last_error = None

            self._session.commit()
            self._session.refresh(heartbeat)
            return heartbeat

        except IntegrityError:
            self._session.rollback()
            heartbeat = self._session.get(
                IndexingWorkerHeartbeat,
                normalized_id,
            )

            if heartbeat is None:
                raise

            heartbeat.status = "running"
            heartbeat.started_at = now
            heartbeat.last_heartbeat_at = now
            heartbeat.stopped_at = None
            heartbeat.last_error = None
            self._session.commit()
            self._session.refresh(heartbeat)
            return heartbeat

        except Exception:
            self._session.rollback()
            raise

    def heartbeat(
        self,
        *,
        worker_id: str,
    ) -> IndexingWorkerHeartbeat:
        normalized_id = self._validate_worker_id(worker_id)
        heartbeat = self._session.get(
            IndexingWorkerHeartbeat,
            normalized_id,
        )

        if heartbeat is None:
            return self.register(worker_id=normalized_id)

        try:
            heartbeat.status = "running"
            heartbeat.last_heartbeat_at = self._now()
            heartbeat.stopped_at = None
            heartbeat.last_error = None
            self._session.commit()
            self._session.refresh(heartbeat)
            return heartbeat
        except Exception:
            self._session.rollback()
            raise

    def mark_stopped(
        self,
        *,
        worker_id: str,
        last_error: str | None = None,
    ) -> IndexingWorkerHeartbeat | None:
        normalized_id = self._validate_worker_id(worker_id)
        heartbeat = self._session.get(
            IndexingWorkerHeartbeat,
            normalized_id,
        )

        if heartbeat is None:
            return None

        try:
            now = self._now()
            heartbeat.status = "stopped"
            heartbeat.last_heartbeat_at = now
            heartbeat.stopped_at = now
            heartbeat.last_error = (
                last_error.strip()[:4000]
                if last_error and last_error.strip()
                else None
            )
            self._session.commit()
            self._session.refresh(heartbeat)
            return heartbeat
        except Exception:
            self._session.rollback()
            raise

    def list_active(
        self,
        *,
        stale_after_seconds: float,
    ) -> list[IndexingWorkerHeartbeat]:
        if stale_after_seconds <= 0:
            raise ValueError(
                "stale_after_seconds 必须大于 0"
            )

        cutoff = self._now() - timedelta(
            seconds=stale_after_seconds
        )
        return list(
            self._session.scalars(
                select(IndexingWorkerHeartbeat)
                .where(
                    IndexingWorkerHeartbeat.status
                    == "running",
                    IndexingWorkerHeartbeat
                    .last_heartbeat_at
                    >= cutoff,
                )
                .order_by(
                    IndexingWorkerHeartbeat
                    .last_heartbeat_at.desc(),
                    IndexingWorkerHeartbeat.worker_id,
                )
            ).all()
        )

    @staticmethod
    def _validate_worker_id(worker_id: str) -> str:
        normalized = worker_id.strip()
        if not normalized:
            raise ValueError("worker_id 不能为空")
        if len(normalized) > 255:
            raise ValueError("worker_id 不能超过 255 个字符")
        return normalized

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
