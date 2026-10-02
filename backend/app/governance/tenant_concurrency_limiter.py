from dataclasses import dataclass
from uuid import UUID, uuid4

from redis import Redis
from redis.exceptions import RedisError


ACQUIRE_SCRIPT = """
local limit = tonumber(ARGV[1])
local lease_ms = tonumber(ARGV[2])
local token = ARGV[3]

local redis_time = redis.call("TIME")
local now_ms = (
    tonumber(redis_time[1]) * 1000
    + math.floor(tonumber(redis_time[2]) / 1000)
)

redis.call(
    "ZREMRANGEBYSCORE",
    KEYS[1],
    "-inf",
    now_ms
)

local active = tonumber(
    redis.call("ZCARD", KEYS[1])
)

if active >= limit then
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
                    - now_ms
                ) / 1000
            )
        )
    end

    redis.call("PEXPIRE", KEYS[1], lease_ms)

    return {
        0,
        active,
        0,
        retry_after
    }
end

local expires_at_ms = now_ms + lease_ms

redis.call(
    "ZADD",
    KEYS[1],
    expires_at_ms,
    token
)

active = active + 1

redis.call("PEXPIRE", KEYS[1], lease_ms)

return {
    1,
    active,
    limit - active,
    0
}
"""


RELEASE_SCRIPT = """
local removed = redis.call(
    "ZREM",
    KEYS[1],
    ARGV[1]
)

if redis.call("ZCARD", KEYS[1]) == 0 then
    redis.call("DEL", KEYS[1])
end

return removed
"""


@dataclass(frozen=True, slots=True)
class ConcurrencyLease:
    tenant_id: UUID
    resource: str
    token: str


@dataclass(frozen=True, slots=True)
class ConcurrencyDecision:
    allowed: bool
    limit: int
    active: int
    remaining: int
    retry_after_seconds: int
    lease: ConcurrencyLease | None


class TenantConcurrencyExceeded(Exception):
    def __init__(
        self,
        decision: ConcurrencyDecision,
    ) -> None:
        super().__init__("租户并发请求超过限制。")
        self.decision = decision


class ConcurrencyLimiterUnavailable(RuntimeError):
    """Redis 无法完成并发槽位操作。"""


class TenantConcurrencyLimiter:
    def __init__(
        self,
        redis_client: Redis,
        *,
        key_prefix: str = "enterprise-kb",
    ) -> None:
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self.acquire_script = (
            redis_client.register_script(
                ACQUIRE_SCRIPT
            )
        )
        self.release_script = (
            redis_client.register_script(
                RELEASE_SCRIPT
            )
        )

    def _key(
        self,
        *,
        tenant_id: UUID,
        resource: str,
    ) -> str:
        if not resource or not all(
            character.isalnum()
            or character in "-_:"
            for character in resource
        ):
            raise ValueError("resource 格式无效")

        return (
            f"{self.key_prefix}:concurrency:"
            f"{{{tenant_id}}}:{resource}"
        )

    def acquire(
        self,
        *,
        tenant_id: UUID,
        resource: str,
        limit: int,
        lease_seconds: int,
    ) -> ConcurrencyDecision:
        if limit < 1:
            raise ValueError("limit 必须是正整数")

        if lease_seconds < 1:
            raise ValueError(
                "lease_seconds 必须是正整数"
            )

        key = self._key(
            tenant_id=tenant_id,
            resource=resource,
        )
        token = uuid4().hex

        try:
            result = self.acquire_script(
                keys=[key],
                args=[
                    limit,
                    lease_seconds * 1000,
                    token,
                ],
            )

            allowed = bool(int(result[0]))
            active = int(result[1])
            remaining = int(result[2])
            retry_after_seconds = int(result[3])
        except RedisError as exc:
            raise ConcurrencyLimiterUnavailable(
                "Redis 并发限制服务不可用。"
            ) from exc
        except (
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            raise ConcurrencyLimiterUnavailable(
                "Redis 返回了无效的并发限制结果。"
            ) from exc

        lease = (
            ConcurrencyLease(
                tenant_id=tenant_id,
                resource=resource,
                token=token,
            )
            if allowed
            else None
        )

        return ConcurrencyDecision(
            allowed=allowed,
            limit=limit,
            active=max(active, 0),
            remaining=max(remaining, 0),
            retry_after_seconds=max(
                retry_after_seconds,
                0,
            ),
            lease=lease,
        )

    def release(
        self,
        lease: ConcurrencyLease,
    ) -> bool:
        key = self._key(
            tenant_id=lease.tenant_id,
            resource=lease.resource,
        )

        try:
            removed = self.release_script(
                keys=[key],
                args=[lease.token],
            )
            return bool(int(removed))
        except RedisError as exc:
            raise ConcurrencyLimiterUnavailable(
                "Redis 并发槽位释放失败。"
            ) from exc
        except (
            TypeError,
            ValueError,
        ) as exc:
            raise ConcurrencyLimiterUnavailable(
                "Redis 返回了无效的槽位释放结果。"
            ) from exc
