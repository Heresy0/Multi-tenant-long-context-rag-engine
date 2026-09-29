from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.dependencies import get_database_session
from ..db.models import User
from .oidc import OidcTokenVerifier, TokenVerificationError

from .principal import Principal


bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )


def get_current_principal(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    session: Annotated[
        Session,
        Depends(get_database_session),
    ],
) -> Principal:
    """验证 Bearer Token 并解析当前应用用户。

    Keycloak 负责证明外部身份；本地 users 表负责决定
    该身份属于哪个租户以及是否已获准使用系统。
    """
    if credentials is None:
        raise _unauthorized("未认证。")

    token_verifier: OidcTokenVerifier = (
        request.app.state.oidc_token_verifier
    )

    try:
        claims = token_verifier.verify(
            credentials.credentials
        )

    except TokenVerificationError as exc:
        raise _unauthorized(str(exc)) from exc

    users = session.scalars(
        select(User).where(
            User.external_subject == claims.subject,
        )
    ).all()

    if len(users) != 1 or users[0].status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前用户尚未开通或已停用。",
        )

    user = users[0]

    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        external_subject=user.external_subject,
    )
