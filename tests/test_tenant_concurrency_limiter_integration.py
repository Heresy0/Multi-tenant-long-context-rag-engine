import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest
from redis import Redis

from backend.app.config import required_env
from backend.app.governance.redis_client import (
    create_redis_client,
    verify_redis_connection,
)
from backend.app.governance.tenant_concurrency_limiter import (
    ConcurrencyLease,
    TenantConcurrencyLimiter,
)


@pytest.fixture
def concurrency_limiter(
) -> Iterator[TenantConcurrencyLimiter]:
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
        "enterprise-kb:concurrency-integration-test:"
        f"{uuid4().hex}"
    )

    verify_redis_connection(redis_client)

    try:
        yield TenantConcurrencyLimiter(
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
def test_real_redis_rejects_at_limit_and_reuses_released_slot(
    concurrency_limiter: TenantConcurrencyLimiter,
) -> None:
    tenant_id = uuid4()

    first = concurrency_limiter.acquire(
        tenant_id=tenant_id,
        resource="qa-release",
        limit=2,
        lease_seconds=60,
    )
    second = concurrency_limiter.acquire(
        tenant_id=tenant_id,
        resource="qa-release",
        limit=2,
        lease_seconds=60,
    )
    denied = concurrency_limiter.acquire(
        tenant_id=tenant_id,
        resource="qa-release",
        limit=2,
        lease_seconds=60,
    )

    assert first.allowed is True
    assert first.remaining == 1
    assert first.lease is not None
    assert second.allowed is True
    assert second.remaining == 0
    assert second.lease is not None
    assert denied.allowed is False
    assert denied.active == 2
    assert denied.lease is None

    assert concurrency_limiter.release(
        first.lease
    ) is True

    replacement = concurrency_limiter.acquire(
        tenant_id=tenant_id,
        resource="qa-release",
        limit=2,
        lease_seconds=60,
    )

    assert replacement.allowed is True
    assert replacement.active == 2


@pytest.mark.integration
def test_real_redis_expired_lease_is_recovered(
    concurrency_limiter: TenantConcurrencyLimiter,
) -> None:
    tenant_id = uuid4()

    acquired = concurrency_limiter.acquire(
        tenant_id=tenant_id,
        resource="qa-expiry",
        limit=1,
        lease_seconds=1,
    )
    denied = concurrency_limiter.acquire(
        tenant_id=tenant_id,
        resource="qa-expiry",
        limit=1,
        lease_seconds=1,
    )

    assert acquired.allowed is True
    assert denied.allowed is False
    assert denied.retry_after_seconds >= 1

    deadline = monotonic() + 3

    while True:
        recovered = concurrency_limiter.acquire(
            tenant_id=tenant_id,
            resource="qa-expiry",
            limit=1,
            lease_seconds=1,
        )

        if recovered.allowed:
            break

        if monotonic() >= deadline:
            pytest.fail("并发租约过期后仍未回收槽位")

        sleep(0.05)

    assert recovered.active == 1
    assert recovered.lease is not None


@pytest.mark.integration
def test_real_redis_keeps_tenant_slots_isolated(
    concurrency_limiter: TenantConcurrencyLimiter,
) -> None:
    tenant_a_id = uuid4()
    tenant_b_id = uuid4()

    tenant_a_first = concurrency_limiter.acquire(
        tenant_id=tenant_a_id,
        resource="qa-isolation",
        limit=1,
        lease_seconds=60,
    )
    tenant_a_second = concurrency_limiter.acquire(
        tenant_id=tenant_a_id,
        resource="qa-isolation",
        limit=1,
        lease_seconds=60,
    )
    tenant_b_first = concurrency_limiter.acquire(
        tenant_id=tenant_b_id,
        resource="qa-isolation",
        limit=1,
        lease_seconds=60,
    )

    assert tenant_a_first.allowed is True
    assert tenant_a_second.allowed is False
    assert tenant_b_first.allowed is True


@pytest.mark.integration
def test_real_redis_acquire_is_atomic_under_concurrency(
    concurrency_limiter: TenantConcurrencyLimiter,
) -> None:
    tenant_id = uuid4()
    request_limit = 3

    def acquire_once(
        _request_number: int,
    ) -> ConcurrencyLease | None:
        return concurrency_limiter.acquire(
            tenant_id=tenant_id,
            resource="qa-concurrent",
            limit=request_limit,
            lease_seconds=60,
        ).lease

    with ThreadPoolExecutor(max_workers=10) as executor:
        leases = list(
            executor.map(acquire_once, range(20))
        )

    acquired_leases = [
        lease
        for lease in leases
        if lease is not None
    ]

    assert len(acquired_leases) == request_limit
    assert len(leases) - len(acquired_leases) == 17

    for lease in acquired_leases:
        assert concurrency_limiter.release(lease) is True
