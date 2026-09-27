from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError


class TokenVerificationError(Exception):
    """访问令牌无法通过本服务的安全校验。"""


@dataclass(frozen=True, slots=True)
class OidcClaims:
    """后端实际使用的、已经验证过的 OIDC 声明。"""

    subject: str
    preferred_username: str | None = None


class OidcTokenVerifier:
    """使用 Keycloak JWKS 验证访问令牌。"""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        jwks_client: Any | None = None,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._jwks_client = jwks_client or PyJWKClient(
            jwks_url,
            cache_keys=True,
        )

    def verify(self, token: str) -> OidcClaims:
        """验签并返回后端允许使用的最小声明集合。"""
        try:
            signing_key = (
                self._jwks_client.get_signing_key_from_jwt(
                    token
                )
            )

            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": [
                        "exp",
                        "iat",
                        "iss",
                        "sub",
                        "aud",
                    ],
                },
            )

        except (InvalidTokenError, PyJWKClientError) as exc:
            raise TokenVerificationError(
                "访问令牌无效或已过期。"
            ) from exc

        subject = payload.get("sub")

        if not isinstance(subject, str) or not subject.strip():
            raise TokenVerificationError(
                "访问令牌缺少有效的用户标识。"
            )

        username = payload.get("preferred_username")

        return OidcClaims(
            subject=subject,
            preferred_username=(
                username
                if isinstance(username, str)
                else None
            ),
        )
