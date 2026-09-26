from fastapi import HTTPException, status

from .principal import Principal


def get_current_principal() -> Principal:
    """取得当前登录用户。

    真实 OIDC 验证将在后续实现。当前默认拒绝所有
    未通过测试依赖覆盖的请求。
    """
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未认证。",
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )