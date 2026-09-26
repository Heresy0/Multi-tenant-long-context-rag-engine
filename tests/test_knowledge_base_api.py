from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from backend.app.api.knowledge_bases import (
    router as knowledge_base_router,
)
from backend.app.db.base import Base
from backend.app.db.models import (
    Department,
    DepartmentMembership,
    KnowledgeBase,
    KnowledgeBaseDepartmentGrant,
    KnowledgeBaseUserGrant,
    Tenant,
    User,
)
from backend.app.db.session import (
    create_session_factory,
)
from backend.app.security.dependencies import (
    get_current_principal,
)
from backend.app.security.principal import Principal


@pytest.fixture
def api_context() -> Iterator[dict]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={
            "check_same_thread": False,
        },
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
    alice_id = uuid4()
    technology_id = uuid4()

    company_kb_id = uuid4()
    technology_kb_id = uuid4()
    hr_kb_id = uuid4()
    special_kb_id = uuid4()

    with session_factory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="测试企业",
            )
        )
        session.flush()

        session.add_all([
            User(
                id=alice_id,
                tenant_id=tenant_id,
                external_subject="alice",
                name="Alice",
            ),
            Department(
                id=technology_id,
                tenant_id=tenant_id,
                name="技术部",
            ),
            KnowledgeBase(
                id=company_kb_id,
                tenant_id=tenant_id,
                name="公司公共知识库",
                visibility="company",
            ),
            KnowledgeBase(
                id=technology_kb_id,
                tenant_id=tenant_id,
                name="技术部知识库",
                visibility="restricted",
            ),
            KnowledgeBase(
                id=hr_kb_id,
                tenant_id=tenant_id,
                name="人力资源部知识库",
                visibility="restricted",
            ),
            KnowledgeBase(
                id=special_kb_id,
                tenant_id=tenant_id,
                name="专项知识库",
                visibility="restricted",
            ),
        ])
        session.flush()

        session.add_all([
            DepartmentMembership(
                tenant_id=tenant_id,
                department_id=technology_id,
                user_id=alice_id,
            ),
            KnowledgeBaseDepartmentGrant(
                tenant_id=tenant_id,
                knowledge_base_id=technology_kb_id,
                department_id=technology_id,
                permission="viewer",
            ),
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=special_kb_id,
                user_id=alice_id,
                permission="editor",
            ),
        ])
        session.commit()

    app = FastAPI()
    app.state.database_session_factory = (
        session_factory
    )
    app.include_router(knowledge_base_router)

    try:
        yield {
            "app": app,
            "principal": Principal(
                user_id=alice_id,
                tenant_id=tenant_id,
                external_subject="alice",
            ),
        }

    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_requires_authentication(
    api_context: dict,
) -> None:
    client = TestClient(api_context["app"])

    response = client.get(
        "/api/knowledge-bases"
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "未认证。"


def test_lists_only_accessible_knowledge_bases(
    api_context: dict,
) -> None:
    app = api_context["app"]

    app.dependency_overrides[
        get_current_principal
    ] = lambda: api_context["principal"]

    try:
        client = TestClient(app)

        response = client.get(
            "/api/knowledge-bases"
        )

    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200

    items = response.json()["items"]

    permissions = {
        item["name"]: item["permission"]
        for item in items
    }

    assert permissions == {
        "公司公共知识库": "viewer",
        "技术部知识库": "viewer",
        "专项知识库": "editor",
    }

    assert "人力资源部知识库" not in permissions