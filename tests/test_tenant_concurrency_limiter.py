from uuid import UUID

import pytest
from redis.exceptions import RedisError

from backend.app.tenant_concurrency_limiter import (
    ConcurrencyLease,
    ConcurrencyLimiterUnavailable,
    TenantConcurrencyLimiter,
)


TENANT_A_ID = UUID(
    "11111111-1111-1111-1111-111111111111"
)
TENANT_B_ID = UUID(
    "22222222-2222-2222-2222-222222222222"
)


class FakeScript:
    def __init__(
        self,
        *,
        result: object = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        keys: list[str],
        args: list[object],
    ) -> object:
        self.calls.append({
            "keys": keys,
            "args": args,
        })

        if self.error is not None:
            raise self.error

        return self.result


class FakeRedis:
    def __init__(
        self,
        acquire_script: FakeScript,
        release_script: FakeScript,
    ) -> None:
        self.scripts = [
            acquire_script,
            release_script,
        ]
        self.registered_sources: list[str] = []

    def register_script(self, source: str) -> FakeScript:
        self.registered_sources.append(source)
        return self.scripts[
            len(self.registered_sources) - 1
        ]


def create_limiter(
    *,
    acquire_result: object = None,
    acquire_error: Exception | None = None,
    release_result: object = 1,
    release_error: Exception | None = None,
) -> tuple[
    TenantConcurrencyLimiter,
    FakeScript,
    FakeScript,
    FakeRedis,
]:
    acquire_script = FakeScript(
        result=acquire_result,
        error=acquire_error,
    )
    release_script = FakeScript(
        result=release_result,
        error=release_error,
    )
    redis_client = FakeRedis(
        acquire_script,
        release_script,
    )
    limiter = TenantConcurrencyLimiter(
        redis_client,  # type: ignore[arg-type]
    )
    return (
        limiter,
        acquire_script,
        release_script,
        redis_client,
    )


def test_acquire_returns_lease_when_slot_is_available(
) -> None:
    limiter, acquire_script, _, redis_client = (
        create_limiter(
            acquire_result=[1, 2, 1, 0]
        )
    )

    decision = limiter.acquire(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=3,
        lease_seconds=180,
    )

    assert decision.allowed is True
    assert decision.limit == 3
    assert decision.active == 2
    assert decision.remaining == 1
    assert decision.retry_after_seconds == 0
    assert decision.lease is not None
    assert decision.lease.tenant_id == TENANT_A_ID
    assert decision.lease.resource == "qa"
    UUID(decision.lease.token)

    assert len(redis_client.registered_sources) == 2
    assert "ZREMRANGEBYSCORE" in (
        redis_client.registered_sources[0]
    )
    assert "ZREM" in redis_client.registered_sources[1]
    assert acquire_script.calls == [{
        "keys": [
            "enterprise-kb:concurrency:"
            "{11111111-1111-1111-1111-111111111111}:qa"
        ],
        "args": [
            3,
            180_000,
            decision.lease.token,
        ],
    }]


def test_acquire_returns_no_lease_when_limit_is_reached(
) -> None:
    limiter, _, _, _ = create_limiter(
        acquire_result=[0, 3, 0, 27]
    )

    decision = limiter.acquire(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=3,
        lease_seconds=180,
    )

    assert decision.allowed is False
    assert decision.active == 3
    assert decision.remaining == 0
    assert decision.retry_after_seconds == 27
    assert decision.lease is None


def test_different_tenants_use_isolated_keys() -> None:
    limiter, acquire_script, _, _ = create_limiter(
        acquire_result=[1, 1, 2, 0]
    )

    limiter.acquire(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=3,
        lease_seconds=180,
    )
    limiter.acquire(
        tenant_id=TENANT_B_ID,
        resource="qa",
        limit=3,
        lease_seconds=180,
    )

    first_key = acquire_script.calls[0]["keys"][0]
    second_key = acquire_script.calls[1]["keys"][0]

    assert first_key != second_key
    assert str(TENANT_A_ID) in first_key
    assert str(TENANT_B_ID) in second_key


def test_release_removes_the_exact_lease_token() -> None:
    limiter, _, release_script, _ = create_limiter(
        acquire_result=[1, 1, 2, 0],
        release_result=1,
    )
    lease = ConcurrencyLease(
        tenant_id=TENANT_A_ID,
        resource="qa",
        token="lease-token",
    )

    released = limiter.release(lease)

    assert released is True
    assert release_script.calls == [{
        "keys": [
            "enterprise-kb:concurrency:"
            "{11111111-1111-1111-1111-111111111111}:qa"
        ],
        "args": ["lease-token"],
    }]


def test_release_returns_false_for_expired_lease() -> None:
    limiter, _, _, _ = create_limiter(
        release_result=0
    )

    released = limiter.release(
        ConcurrencyLease(
            tenant_id=TENANT_A_ID,
            resource="qa",
            token="expired-token",
        )
    )

    assert released is False


@pytest.mark.parametrize(
    ("limit", "lease_seconds", "resource", "message"),
    [
        (0, 180, "qa", "limit 必须是正整数"),
        (3, 0, "qa", "lease_seconds 必须是正整数"),
        (3, 180, "", "resource 格式无效"),
        (3, 180, "qa/other", "resource 格式无效"),
        (3, 180, "qa other", "resource 格式无效"),
    ],
)
def test_invalid_acquire_parameters_do_not_call_redis(
    limit: int,
    lease_seconds: int,
    resource: str,
    message: str,
) -> None:
    limiter, acquire_script, _, _ = create_limiter(
        acquire_result=[1, 1, 2, 0]
    )

    with pytest.raises(ValueError, match=message):
        limiter.acquire(
            tenant_id=TENANT_A_ID,
            resource=resource,
            limit=limit,
            lease_seconds=lease_seconds,
        )

    assert acquire_script.calls == []


def test_acquire_redis_error_becomes_unavailable() -> None:
    original_error = RedisError("connection lost")
    limiter, _, _, _ = create_limiter(
        acquire_error=original_error
    )

    with pytest.raises(
        ConcurrencyLimiterUnavailable,
        match="Redis 并发限制服务不可用",
    ) as exc_info:
        limiter.acquire(
            tenant_id=TENANT_A_ID,
            resource="qa",
            limit=3,
            lease_seconds=180,
        )

    assert exc_info.value.__cause__ is original_error


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        ["invalid", 1, 2, 0],
        [1, "invalid", 2, 0],
        [1, 1, "invalid", 0],
        [1, 1, 2, "invalid"],
    ],
)
def test_invalid_acquire_result_becomes_unavailable(
    result: object,
) -> None:
    limiter, _, _, _ = create_limiter(
        acquire_result=result
    )

    with pytest.raises(
        ConcurrencyLimiterUnavailable,
        match="Redis 返回了无效的并发限制结果",
    ):
        limiter.acquire(
            tenant_id=TENANT_A_ID,
            resource="qa",
            limit=3,
            lease_seconds=180,
        )


def test_release_redis_error_becomes_unavailable() -> None:
    original_error = RedisError("connection lost")
    limiter, _, _, _ = create_limiter(
        release_error=original_error
    )

    with pytest.raises(
        ConcurrencyLimiterUnavailable,
        match="Redis 并发槽位释放失败",
    ) as exc_info:
        limiter.release(
            ConcurrencyLease(
                tenant_id=TENANT_A_ID,
                resource="qa",
                token="lease-token",
            )
        )

    assert exc_info.value.__cause__ is original_error


@pytest.mark.parametrize(
    "result",
    [None, "invalid", object()],
)
def test_invalid_release_result_becomes_unavailable(
    result: object,
) -> None:
    limiter, _, _, _ = create_limiter(
        release_result=result
    )

    with pytest.raises(
        ConcurrencyLimiterUnavailable,
        match="Redis 返回了无效的槽位释放结果",
    ):
        limiter.release(
            ConcurrencyLease(
                tenant_id=TENANT_A_ID,
                resource="qa",
                token="lease-token",
            )
        )


def test_negative_script_counts_are_clamped() -> None:
    limiter, _, _, _ = create_limiter(
        acquire_result=[1, -2, -1, -5]
    )

    decision = limiter.acquire(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=3,
        lease_seconds=180,
    )

    assert decision.active == 0
    assert decision.remaining == 0
    assert decision.retry_after_seconds == 0
