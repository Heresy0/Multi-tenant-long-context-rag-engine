from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import Tenant, User
from backend.app.db.session import create_session_factory
from backend.app.security.dependencies import (
    get_current_principal,
)
from backend.app.security.oidc import (
    OidcClaims,
    TokenVerificationError,
)
from backend.app.security.principal import Principal


class StubTokenVerifier:
    def __init__(
        self,
        *,
        subject: str = "keycloak-alice",
        reject: bool = False,
    ) -> None:
        self._subject = subject
        self._reject = reject

    def verify(self, _token: str) -> OidcClaims:
        if self._reject:
            raise TokenVerificationError("访问令牌无效或已过期。")

        return OidcClaims(
            subject=self._subject,
            preferred_username="alice",
        )


@pytest.fixture
def protected_api() -> Iterator[dict]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(
        dbapi_connection,
        _connection_record,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    tenant_id = uuid4()
    user_id = uuid4()

    with session_factory() as session:
        session.add(Tenant(id=tenant_id, name="测试企业"))
        session.flush()
        session.add(
            User(
                id=user_id,
                tenant_id=tenant_id,
                external_subject="keycloak-alice",
                name="Alice",
            )
        )
        session.commit()

    app = FastAPI()
    app.state.database_session_factory = session_factory
    app.state.oidc_token_verifier = StubTokenVerifier()

    @app.get("/protected")
    def protected(
        principal: Principal = Depends(
            get_current_principal
        ),
    ) -> dict[str, str]:
        return {
            "user_id": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
        }

    try:
        yield {
            "app": app,
            "user_id": user_id,
            "tenant_id": tenant_id,
        }

    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_maps_verified_subject_to_local_user(
    protected_api: dict,
) -> None:
    client = TestClient(protected_api["app"])

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(protected_api["user_id"]),
        "tenant_id": str(protected_api["tenant_id"]),
    }


def test_rejects_invalid_token(
    protected_api: dict,
) -> None:
    app = protected_api["app"]
    app.state.oidc_token_verifier = StubTokenVerifier(
        reject=True
    )
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_rejects_unprovisioned_user(
    protected_api: dict,
) -> None:
    app = protected_api["app"]
    app.state.oidc_token_verifier = StubTokenVerifier(
        subject="unknown-subject"
    )
    client = TestClient(app)

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer valid-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "当前用户尚未开通或已停用。"
    )
