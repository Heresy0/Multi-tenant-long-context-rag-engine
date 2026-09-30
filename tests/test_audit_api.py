from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.api.knowledge_bases import (
    router as knowledge_base_router,
)
from backend.app.audit_service import AuditService
from backend.app.db.base import Base
from backend.app.db.models import (
    KnowledgeBase,
    KnowledgeBaseUserGrant,
    Tenant,
    User,
)
from backend.app.db.session import create_session_factory
from backend.app.security.dependencies import (
    get_current_principal,
)
from backend.app.security.principal import Principal


@dataclass(frozen=True, slots=True)
class AuditApiContext:
    app: FastAPI
    tenant_id: UUID
    knowledge_base_id: UUID
    admin: Principal
    editor: Principal


@pytest.fixture
def api_context() -> Iterator[AuditApiContext]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    knowledge_base_id = uuid4()
    admin_id = uuid4()
    editor_id = uuid4()

    with session_factory() as session:
        session.add_all([
            Tenant(id=tenant_id, name="测试企业"),
            Tenant(id=other_tenant_id, name="其他企业"),
        ])
        session.flush()
        session.add_all([
            User(
                id=admin_id,
                tenant_id=tenant_id,
                external_subject="admin",
                name="管理员",
            ),
            User(
                id=editor_id,
                tenant_id=tenant_id,
                external_subject="editor",
                name="编辑者",
            ),
            KnowledgeBase(
                id=knowledge_base_id,
                tenant_id=tenant_id,
                name="技术部知识库",
            ),
        ])
        session.flush()
        session.add_all([
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                user_id=admin_id,
                permission="admin",
            ),
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                user_id=editor_id,
                permission="editor",
            ),
        ])
        session.commit()
        AuditService(session).record(
            tenant_id=tenant_id,
            actor_user_id=editor_id,
            knowledge_base_id=knowledge_base_id,
            action="document.upload_requested",
            resource_type="document",
            resource_id=uuid4(),
            outcome="success",
            request_id="request-42",
            details={"target_version": 1},
        )
        AuditService(session).record(
            tenant_id=other_tenant_id,
            actor_user_id=None,
            knowledge_base_id=knowledge_base_id,
            action="qa.queried",
            resource_type="knowledge_base",
            outcome="denied",
        )

    app = FastAPI()
    app.state.database_session_factory = session_factory
    app.include_router(knowledge_base_router)

    try:
        yield AuditApiContext(
            app=app,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            admin=Principal(
                user_id=admin_id,
                tenant_id=tenant_id,
                external_subject="admin",
            ),
            editor=Principal(
                user_id=editor_id,
                tenant_id=tenant_id,
                external_subject="editor",
            ),
        )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _url(context: AuditApiContext) -> str:
    return (
        f"/api/knowledge-bases/{context.knowledge_base_id}"
        "/audit-events"
    )


def test_admin_can_list_only_tenant_scoped_events(
    api_context: AuditApiContext,
) -> None:
    api_context.app.dependency_overrides[
        get_current_principal
    ] = lambda: api_context.admin

    response = TestClient(api_context.app).get(
        _url(api_context),
        params={"action": "document.upload_requested"},
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    item = response.json()["items"][0]
    assert item["action"] == "document.upload_requested"
    assert item["request_id"] == "request-42"
    assert item["details"] == {"target_version": 1}


def test_editor_cannot_read_audit_events(
    api_context: AuditApiContext,
) -> None:
    api_context.app.dependency_overrides[
        get_current_principal
    ] = lambda: api_context.editor

    response = TestClient(api_context.app).get(
        _url(api_context)
    )

    assert response.status_code == 403
