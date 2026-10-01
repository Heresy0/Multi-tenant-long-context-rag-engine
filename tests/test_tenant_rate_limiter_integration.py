import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest
from redis import Redis

from backend.app.config import required_env
from backend.app.redis_client import (
    create_redis_client,
    verify_redis_connection,
)
from backend.app.tenant_rate_limiter import (
    TenantRateLimiter,
)


@pytest.fixture
def redis_limiter() -> Iterator[TenantRateLimiter]:
    if (
        os.getenv("RUN_REDIS_INTEGRATION_TESTS")
        != "1"
    ):
        pytest.skip("未启用 Redis 集成测试")

    redis_client: Redis = create_redis_client(
        required_env("REDIS_URL"),
        connect_timeout_seconds=2,
        socket_timeout_seconds=2,
    )
    key_prefix = (
        "enterprise-kb:integration-test:"
        f"{uuid4().hex}"
    )

    verify_redis_connection(redis_client)

    try:
        yield TenantRateLimiter(
            redis_client,
            key_prefix=key_prefix,
        )
    finally:
        keys = list(
            redis_client.scan_iter(
                match=f"{key_prefix}:*"
            )
        )

        if keys:
            redis_client.delete(*keys)

        redis_client.close()


@pytest.mark.integration
def test_real_redis_enforces_limit_and_recovers_after_window(
    redis_limiter: TenantRateLimiter,
) -> None:
    tenant_id = uuid4()

    first = redis_limiter.consume(
        tenant_id=tenant_id,
        resource="qa-window",
        limit=2,
        window_seconds=1,
    )
    second = redis_limiter.consume(
        tenant_id=tenant_id,
        resource="qa-window",
        limit=2,
        window_seconds=1,
    )
    denied = redis_limiter.consume(
        tenant_id=tenant_id,
        resource="qa-window",
        limit=2,
        window_seconds=1,
    )

    assert first.allowed is True
    assert first.remaining == 1
    assert second.allowed is True
    assert second.remaining == 0
    assert denied.allowed is False
    assert denied.remaining == 0
    assert denied.reset_after_seconds >= 1

    deadline = monotonic() + 3

    while True:
        recovered = redis_limiter.consume(
            tenant_id=tenant_id,
            resource="qa-window",
            limit=2,
            window_seconds=1,
        )

        if recovered.allowed:
            break

        if monotonic() >= deadline:
            pytest.fail(
                "滑动窗口过期后仍未恢复请求配额"
            )

        sleep(0.05)

    assert recovered.remaining == 1


@pytest.mark.integration
def test_real_redis_keeps_tenant_windows_isolated(
    redis_limiter: TenantRateLimiter,
) -> None:
    tenant_a_id = uuid4()
    tenant_b_id = uuid4()

    tenant_a_first = redis_limiter.consume(
        tenant_id=tenant_a_id,
        resource="qa-isolation",
        limit=1,
        window_seconds=60,
    )
    tenant_a_second = redis_limiter.consume(
        tenant_id=tenant_a_id,
        resource="qa-isolation",
        limit=1,
        window_seconds=60,
    )
    tenant_b_first = redis_limiter.consume(
        tenant_id=tenant_b_id,
        resource="qa-isolation",
        limit=1,
        window_seconds=60,
    )

    assert tenant_a_first.allowed is True
    assert tenant_a_second.allowed is False
    assert tenant_b_first.allowed is True


@pytest.mark.integration
def test_real_redis_limit_is_atomic_under_concurrency(
    redis_limiter: TenantRateLimiter,
) -> None:
    tenant_id = uuid4()
    request_limit = 5

    def consume_once(_request_number: int) -> bool:
        return redis_limiter.consume(
            tenant_id=tenant_id,
            resource="qa-concurrent",
            limit=request_limit,
            window_seconds=60,
        ).allowed

    with ThreadPoolExecutor(max_workers=10) as executor:
        allowed_results = list(
            executor.map(consume_once, range(20))
        )

    assert sum(allowed_results) == request_limit
    assert allowed_results.count(False) == 15
