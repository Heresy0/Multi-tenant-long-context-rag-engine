from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.app.security.oidc import (
    OidcTokenVerifier,
    TokenVerificationError,
)


ISSUER = (
    "http://127.0.0.1:8080/realms/enterprise-knowledge"
)
AUDIENCE = "enterprise-knowledge-api"


class StubSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class StubJwksClient:
    def __init__(self, key) -> None:
        self._key = key

    def get_signing_key_from_jwt(
        self,
        _token: str,
    ) -> StubSigningKey:
        return StubSigningKey(self._key)


@pytest.fixture(scope="module")
def rsa_keys():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    return private_key, private_key.public_key()


def create_verifier(public_key) -> OidcTokenVerifier:
    return OidcTokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://unused.example/jwks",
        jwks_client=StubJwksClient(public_key),
    )


def create_token(
    private_key,
    *,
    audience: str | list[str] = AUDIENCE,
    expires_delta: timedelta = timedelta(minutes=5),
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": audience,
            "sub": "keycloak-alice",
            "preferred_username": "alice",
            "iat": now,
            "exp": now + expires_delta,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def test_accepts_valid_keycloak_access_token(
    rsa_keys,
) -> None:
    private_key, public_key = rsa_keys
    verifier = create_verifier(public_key)
    token = create_token(
        private_key,
        audience=[AUDIENCE, "account"],
    )

    claims = verifier.verify(token)

    assert claims.subject == "keycloak-alice"
    assert claims.preferred_username == "alice"


def test_rejects_wrong_audience(rsa_keys) -> None:
    private_key, public_key = rsa_keys
    verifier = create_verifier(public_key)
    token = create_token(
        private_key,
        audience="another-api",
    )

    with pytest.raises(TokenVerificationError):
        verifier.verify(token)


def test_rejects_expired_token(rsa_keys) -> None:
    private_key, public_key = rsa_keys
    verifier = create_verifier(public_key)
    token = create_token(
        private_key,
        expires_delta=timedelta(seconds=-1),
    )

    with pytest.raises(TokenVerificationError):
        verifier.verify(token)
