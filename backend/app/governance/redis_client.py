from redis import Redis


def create_redis_client(
    redis_url: str,
    *,
    connect_timeout_seconds: float,
    socket_timeout_seconds: float,
) -> Redis:
    """创建线程安全、带连接池的 Redis 客户端。"""
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=(
            connect_timeout_seconds
        ),
        socket_timeout=socket_timeout_seconds,
        health_check_interval=30,
    )


def verify_redis_connection(
    redis_client: Redis,
) -> None:
    """启动阶段验证 Redis 是否可用。"""
    if redis_client.ping() is not True:
        raise RuntimeError("Redis 连接验证失败。")
