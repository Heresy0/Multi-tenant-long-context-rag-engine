from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from ..indexing_observability_service import (
    IndexingObservabilityService,
)
from ..metrics import (
    record_metrics_refresh_failure,
    refresh_indexing_metrics,
    render_metrics,
)
from ..system_schemas import (
    IndexingHealthResponse,
    IndexingQueueStatus,
    LivenessResponse,
    ReadinessResponse,
)


router = APIRouter(tags=["system"])


@router.get(
    "/metrics",
    include_in_schema=False,
)
def prometheus_metrics(request: Request) -> Response:
    """导出 Prometheus 指标；数据库失败时仍返回进程指标。"""
    try:
        session_factory: sessionmaker[Session] = (
            request.app.state.database_session_factory
        )
        settings = request.app.state.settings

        with session_factory() as session:
            snapshot = IndexingObservabilityService(
                session=session
            ).snapshot(
                worker_stale_after_seconds=(
                    settings.indexing_worker_stale_seconds
                )
            )
        refresh_indexing_metrics(snapshot)
    except Exception:
        record_metrics_refresh_failure()

    payload, content_type = render_metrics()
    return Response(
        content=payload,
        media_type=content_type,
    )


@router.get(
    "/health",
    response_model=LivenessResponse,
)
@router.get(
    "/health/live",
    response_model=LivenessResponse,
)
def liveness() -> LivenessResponse:
    """只确认 API 进程能够响应。"""
    return LivenessResponse(status="ok")


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
)
def readiness(
    request: Request,
    response: Response,
) -> ReadinessResponse:
    """确认 API 能够连接数据库和 Redis。"""
    database_status = "up"
    redis_status = "up"

    try:
        with (
            request.app.state.database_engine.connect()
            as connection
        ):
            connection.execute(text("SELECT 1"))
    except Exception:
        database_status = "down"

    try:
        if (
            request.app.state.redis_client.ping()
            is not True
        ):
            redis_status = "down"
    except Exception:
        redis_status = "down"

    if (
        database_status != "up"
        or redis_status != "up"
    ):
        response.status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
        )

    return ReadinessResponse(
        status=(
            "ok"
            if (
                database_status == "up"
                and redis_status == "up"
            )
            else "unavailable"
        ),
        database=database_status,
        redis=redis_status,
    )


@router.get(
    "/health/indexing",
    response_model=IndexingHealthResponse,
)
def indexing_health(
    request: Request,
    response: Response,
) -> IndexingHealthResponse:
    """返回索引 Worker 心跳和队列状态。"""
    empty_queue = IndexingQueueStatus(
        queued=0,
        running=0,
        succeeded=0,
        failed=0,
        oldest_queued_seconds=None,
        succeeded_last_24_hours=0,
        failed_last_24_hours=0,
    )

    try:
        session_factory: sessionmaker[Session] = (
            request.app.state.database_session_factory
        )
        settings = request.app.state.settings

        with session_factory() as session:
            snapshot = IndexingObservabilityService(
                session=session
            ).snapshot(
                worker_stale_after_seconds=(
                    settings.indexing_worker_stale_seconds
                )
            )
    except Exception:
        response.status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return IndexingHealthResponse(
            status="unavailable",
            active_workers=0,
            last_heartbeat_at=None,
            queue=empty_queue,
        )

    if snapshot.status != "ok":
        response.status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
        )

    return IndexingHealthResponse(
        status=snapshot.status,
        active_workers=snapshot.active_workers,
        last_heartbeat_at=snapshot.last_heartbeat_at,
        queue=IndexingQueueStatus(
            queued=snapshot.queue.queued,
            running=snapshot.queue.running,
            succeeded=snapshot.queue.succeeded,
            failed=snapshot.queue.failed,
            oldest_queued_seconds=(
                snapshot.queue.oldest_queued_seconds
            ),
            succeeded_last_24_hours=(
                snapshot.queue.succeeded_last_24_hours
            ),
            failed_last_24_hours=(
                snapshot.queue.failed_last_24_hours
            ),
        ),
    )
