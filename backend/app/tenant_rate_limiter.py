from dataclasses import dataclass
from uuid import UUID, uuid4

from redis import Redis
from redis.exceptions import RedisError


SLIDING_WINDOW_SCRIPT = """
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]

local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)
local cutoff_ms = now_ms - window_ms

redis.call(
    "ZREMRANGEBYSCORE",
    KEYS[1],
    "-inf",
    cutoff_ms
)

local count = tonumber(
    redis.call("ZCARD", KEYS[1])
)

if count >= limit then
    local oldest = redis.call(
        "ZRANGE",
        KEYS[1],
        0,
        0,
        "WITHSCORES"
    )

    local retry_after = 1

    if #oldest >= 2 then
        retry_after = math.max(
            1,
            math.ceil(
                (
                    tonumber(oldest[2])
                    + window_ms
                    - now_ms
                ) / 1000
            )
        )
    end

    redis.call("PEXPIRE", KEYS[1], window_ms)

    return {
        0,
        0,
        retry_after
    }
end

redis.call(
    "ZADD",
    KEYS[1],
    now_ms,
    member
)

count = count + 1

redis.call("PEXPIRE", KEYS[1], window_ms)

local oldest = redis.call(
    "ZRANGE",
    KEYS[1],
    0,
    0,
    "WITHSCORES"
)

local reset_after = math.ceil(
    (
        tonumber(oldest[2])
        + window_ms
        - now_ms
    ) / 1000
)

return {
    1,
    limit - count,
    math.max(1, reset_after)
}
"""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after_seconds: int


class TenantRateLimitExceeded(Exception):
    def __init__(
        self,
        decision: RateLimitDecision,
    ) -> None:
        super().__init__("租户请求频率超过限制。")
        self.decision = decision


class RateLimiterUnavailable(RuntimeError):
    """Redis 无法完成限流判断。"""


class TenantRateLimiter:
    def __init__(
        self,
        redis_client: Redis,
        *,
        key_prefix: str = "enterprise-kb",
    ) -> None:
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self.script = redis_client.register_script(
            SLIDING_WINDOW_SCRIPT
        )

    def consume(
        self,
        *,
        tenant_id: UUID,
        resource: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        if limit < 1:
            raise ValueError("limit 必须是正整数")

        if window_seconds < 1:
            raise ValueError(
                "window_seconds 必须是正整数"
            )

        if not resource or not all(
            character.isalnum()
            or character in "-_:"
            for character in resource
        ):
            raise ValueError("resource 格式无效")

        key = (
            f"{self.key_prefix}:rate-limit:"
            f"{{{tenant_id}}}:{resource}"
        )

        try:
            result = self.script(
                keys=[key],
                args=[
                    window_seconds * 1000,
                    limit,
                    uuid4().hex,
                ],
            )

            allowed = bool(int(result[0]))
            remaining = int(result[1])
            reset_after_seconds = int(result[2])
        except RedisError as exc:
            raise RateLimiterUnavailable(
                "Redis 限流服务不可用。"
            ) from exc
        except (
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise RateLimiterUnavailable(
                "Redis 返回了无效的限流结果。"
            ) from exc

        return RateLimitDecision(
            allowed=allowed,
            limit=limit,
            remaining=max(remaining, 0),
            reset_after_seconds=max(
                reset_after_seconds,
                1,
            ),
        )