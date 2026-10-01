from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    REGISTRY,
    generate_latest,
)

from .answer_models import AnswerTimings
from .indexing_observability_service import (
    IndexingHealthSnapshot,
)


DURATION_BUCKETS = (
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2,
    5,
    10,
    20,
    40,
    60,
    120,
)

HTTP_REQUESTS = Counter(
    "enterprise_http_requests_total",
    "HTTP 请求总数。",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "enterprise_http_request_duration_seconds",
    "HTTP 请求耗时。",
    ("method", "route"),
    buckets=DURATION_BUCKETS,
)
QA_REQUESTS = Counter(
    "enterprise_qa_requests_total",
    "知识库问答请求总数。",
    ("outcome", "answerable"),
)
QA_REQUEST_DURATION = Histogram(
    "enterprise_qa_request_duration_seconds",
    "知识库问答端到端耗时。",
    ("outcome",),
    buckets=DURATION_BUCKETS,
)
QA_STAGE_DURATION = Histogram(
    "enterprise_qa_stage_duration_seconds",
    "知识库问答各阶段耗时。",
    ("stage",),
    buckets=DURATION_BUCKETS,
)
INDEXING_JOBS = Counter(
    "enterprise_indexing_jobs_total",
    "索引任务执行结果总数。",
    ("outcome",),
)
INDEXING_JOB_DURATION = Histogram(
    "enterprise_indexing_job_duration_seconds",
    "单次索引任务执行耗时。",
    ("outcome",),
    buckets=DURATION_BUCKETS,
)
INDEXING_RECOVERED_JOBS = Counter(
    "enterprise_indexing_recovered_jobs_total",
    "恢复的超时索引任务总数。",
)
INDEXING_CLEANED_CANDIDATES = Counter(
    "enterprise_indexing_cleaned_candidates_total",
    "清理的过期候选文件总数。",
)
TENANT_UPLOAD_QUOTA_REJECTIONS = Counter(
    "enterprise_tenant_upload_quota_rejections_total",
    "租户文档上传因配额不足被拒绝的次数。",
    ("resource",),
)
INDEXING_WORKERS_ACTIVE = Gauge(
    "enterprise_indexing_workers_active",
    "最近仍在发送心跳的索引 Worker 数量。",
)
INDEXING_QUEUE_JOBS = Gauge(
    "enterprise_indexing_queue_jobs",
    "按状态统计的索引任务数量。",
    ("status",),
)
INDEXING_OLDEST_QUEUED_SECONDS = Gauge(
    "enterprise_indexing_oldest_queued_seconds",
    "最老排队任务已经等待的秒数。",
)
METRICS_REFRESH_FAILURES = Counter(
    "enterprise_metrics_refresh_failures_total",
    "刷新数据库派生指标失败的次数。",
)


def record_qa_request(
    *,
    outcome: str,
    answerable: bool | None,
    duration_seconds: float,
    timings: AnswerTimings | None = None,
) -> None:
    answerable_label = (
        str(answerable).lower()
        if answerable is not None
        else "unknown"
    )
    QA_REQUESTS.labels(
        outcome=outcome,
        answerable=answerable_label,
    ).inc()
    QA_REQUEST_DURATION.labels(
        outcome=outcome
    ).observe(max(duration_seconds, 0.0))

    if timings is None:
        return

    for stage, milliseconds in timings.model_dump().items():
        if milliseconds is None:
            continue
        QA_STAGE_DURATION.labels(stage=stage).observe(
            max(float(milliseconds) / 1000, 0.0)
        )


def record_indexing_job(
    *,
    outcome: str,
    duration_seconds: float,
) -> None:
    INDEXING_JOBS.labels(outcome=outcome).inc()
    INDEXING_JOB_DURATION.labels(
        outcome=outcome
    ).observe(max(duration_seconds, 0.0))


def record_recovered_jobs(count: int) -> None:
    if count > 0:
        INDEXING_RECOVERED_JOBS.inc(count)


def record_cleaned_candidates(count: int) -> None:
    if count > 0:
        INDEXING_CLEANED_CANDIDATES.inc(count)


def record_tenant_upload_quota_rejection(
    resource: str,
) -> None:
    TENANT_UPLOAD_QUOTA_REJECTIONS.labels(
        resource=resource
    ).inc()


def refresh_indexing_metrics(
    snapshot: IndexingHealthSnapshot,
) -> None:
    INDEXING_WORKERS_ACTIVE.set(snapshot.active_workers)
    queue = snapshot.queue

    for status, count in (
        ("queued", queue.queued),
        ("running", queue.running),
        ("succeeded", queue.succeeded),
        ("failed", queue.failed),
    ):
        INDEXING_QUEUE_JOBS.labels(status=status).set(count)

    INDEXING_OLDEST_QUEUED_SECONDS.set(
        queue.oldest_queued_seconds or 0
    )


def record_metrics_refresh_failure() -> None:
    METRICS_REFRESH_FAILURES.inc()


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


async def observe_http_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """记录低基数 HTTP 指标，忽略 Prometheus 自身抓取。"""
    if request.url.path == "/metrics":
        return await call_next(request)

    started = perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        method = request.method
        HTTP_REQUESTS.labels(
            method=method,
            route=route_path,
            status=str(status_code),
        ).inc()
        HTTP_REQUEST_DURATION.labels(
            method=method,
            route=route_path,
        ).observe(max(perf_counter() - started, 0.0))
