from uuid import UUID

import pytest
from redis.exceptions import RedisError

from backend.app.tenant_rate_limiter import (
    RateLimiterUnavailable,
    TenantRateLimiter,
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
    def __init__(self, script: FakeScript) -> None:
        self.script = script
        self.registered_sources: list[str] = []

    def register_script(self, source: str) -> FakeScript:
        self.registered_sources.append(source)
        return self.script


def create_limiter(
    *,
    result: object = None,
    error: Exception | None = None,
) -> tuple[TenantRateLimiter, FakeScript, FakeRedis]:
    script = FakeScript(
        result=result,
        error=error,
    )
    redis_client = FakeRedis(script)
    limiter = TenantRateLimiter(
        redis_client,  # type: ignore[arg-type]
    )
    return limiter, script, redis_client


def test_allowed_result_is_converted_to_decision() -> None:
    limiter, script, redis_client = create_limiter(
        result=[1, 7, 42]
    )

    decision = limiter.consume(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=10,
        window_seconds=60,
    )

    assert decision.allowed is True
    assert decision.limit == 10
    assert decision.remaining == 7
    assert decision.reset_after_seconds == 42
    assert len(redis_client.registered_sources) == 1
    assert "ZREMRANGEBYSCORE" in (
        redis_client.registered_sources[0]
    )
    assert len(script.calls) == 1

    call = script.calls[0]
    assert call["keys"] == [
        "enterprise-kb:rate-limit:"
        "{11111111-1111-1111-1111-111111111111}:qa"
    ]
    assert call["args"][:2] == [60_000, 10]

    member = call["args"][2]
    assert isinstance(member, str)
    assert len(member) == 32
    UUID(member)


def test_denied_result_is_converted_to_decision() -> None:
    limiter, _, _ = create_limiter(
        result=[0, 0, 17]
    )

    decision = limiter.consume(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=60,
        window_seconds=60,
    )

    assert decision.allowed is False
    assert decision.limit == 60
    assert decision.remaining == 0
    assert decision.reset_after_seconds == 17


def test_each_tenant_uses_an_isolated_redis_key() -> None:
    limiter, script, _ = create_limiter(
        result=[1, 59, 60]
    )

    limiter.consume(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=60,
        window_seconds=60,
    )
    limiter.consume(
        tenant_id=TENANT_B_ID,
        resource="qa",
        limit=60,
        window_seconds=60,
    )

    first_key = script.calls[0]["keys"][0]
    second_key = script.calls[1]["keys"][0]

    assert first_key != second_key
    assert str(TENANT_A_ID) in first_key
    assert str(TENANT_B_ID) in second_key


def test_each_request_uses_a_unique_sorted_set_member() -> None:
    limiter, script, _ = create_limiter(
        result=[1, 59, 60]
    )

    for _ in range(2):
        limiter.consume(
            tenant_id=TENANT_A_ID,
            resource="qa",
            limit=60,
            window_seconds=60,
        )

    first_member = script.calls[0]["args"][2]
    second_member = script.calls[1]["args"][2]

    assert first_member != second_member


@pytest.mark.parametrize(
    ("limit", "window_seconds", "resource", "message"),
    [
        (0, 60, "qa", "limit 必须是正整数"),
        (60, 0, "qa", "window_seconds 必须是正整数"),
        (60, 60, "", "resource 格式无效"),
        (60, 60, "qa/other", "resource 格式无效"),
        (60, 60, "qa other", "resource 格式无效"),
    ],
)
def test_invalid_configuration_is_rejected_before_redis_call(
    limit: int,
    window_seconds: int,
    resource: str,
    message: str,
) -> None:
    limiter, script, _ = create_limiter(
        result=[1, 59, 60]
    )

    with pytest.raises(ValueError, match=message):
        limiter.consume(
            tenant_id=TENANT_A_ID,
            resource=resource,
            limit=limit,
            window_seconds=window_seconds,
        )

    assert script.calls == []


def test_redis_error_is_exposed_as_service_unavailable() -> None:
    original_error = RedisError("connection lost")
    limiter, _, _ = create_limiter(
        error=original_error
    )

    with pytest.raises(
        RateLimiterUnavailable,
        match="Redis 限流服务不可用",
    ) as exc_info:
        limiter.consume(
            tenant_id=TENANT_A_ID,
            resource="qa",
            limit=60,
            window_seconds=60,
        )

    assert exc_info.value.__cause__ is original_error


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        ["invalid", 0, 1],
        [1, "invalid", 1],
        [1, 0, "invalid"],
    ],
)
def test_invalid_script_result_is_exposed_as_unavailable(
    result: object,
) -> None:
    limiter, _, _ = create_limiter(result=result)

    with pytest.raises(
        RateLimiterUnavailable,
        match="Redis 返回了无效的限流结果",
    ):
        limiter.consume(
            tenant_id=TENANT_A_ID,
            resource="qa",
            limit=60,
            window_seconds=60,
        )


def test_negative_values_from_script_are_clamped() -> None:
    limiter, _, _ = create_limiter(
        result=[1, -3, -5]
    )

    decision = limiter.consume(
        tenant_id=TENANT_A_ID,
        resource="qa",
        limit=60,
        window_seconds=60,
    )

    assert decision.remaining == 0
    assert decision.reset_after_seconds == 1
