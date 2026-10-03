from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

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
from backend.app.security.authorization import (
    AuthorizationDenied,
    AuthorizationService,
)
from backend.app.security.principal import Principal
from backend.app.security.retrieval_scope import RetrievalScope


def test_all_accessible_scope_is_derived_from_public_department_and_direct_grants(session, permission_data):
    ids = permission_data
    principal = Principal(ids["alice"], ids["tenant_a"], "alice")
    authorization = AuthorizationService(session)
    scope = authorization.require_retrieval_scope(principal=principal)
    assert scope.knowledge_base_id is None
    assert scope.knowledge_base_ids == {ids["company_kb"], ids["technology_kb"], ids["special_kb"]}
    assert {kb_id for kb_id, _ in scope.knowledge_base_names} == scope.knowledge_base_ids
    session.get(User, principal.user_id).status = "disabled"
    session.commit()
    with pytest.raises(AuthorizationDenied):
        authorization.require_retrieval_scope(principal=principal)


def test_retrieval_scope_freezes_ids_and_rejects_mixed_single_scope():
    tenant, kb, other = uuid4(), uuid4(), uuid4()
    ids = {kb, other}
    scope = RetrievalScope(tenant, knowledge_base_ids=ids)
    ids.clear()
    assert scope.knowledge_base_ids == {kb, other}
    assert RetrievalScope(tenant, kb).knowledge_base_ids == {kb}
    with pytest.raises(ValueError):
        RetrievalScope(tenant, kb, frozenset({other}))


@pytest.fixture
def session() -> Iterator[Session]:
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

    with Session(engine) as database_session:
        yield database_session

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def permission_data(
    session: Session,
) -> dict:
    ids = {
        "tenant_a": uuid4(),
        "tenant_b": uuid4(),
        "alice": uuid4(),
        "bob": uuid4(),
        "carol": uuid4(),
        "technology": uuid4(),
        "hr": uuid4(),
        "finance": uuid4(),
        "company_kb": uuid4(),
        "technology_kb": uuid4(),
        "hr_kb": uuid4(),
        "finance_kb": uuid4(),
        "special_kb": uuid4(),
    }

    session.add_all([
        Tenant(
            id=ids["tenant_a"],
            name="企业 A",
        ),
        Tenant(
            id=ids["tenant_b"],
            name="企业 B",
        ),
    ])

    # 没有配置 ORM relationship 时，显式分阶段写入，
    # 确保租户父记录先于用户、部门和知识库存在。
    session.flush()

    session.add_all([
        User(
            id=ids["alice"],
            tenant_id=ids["tenant_a"],
            external_subject="alice",
            name="Alice",
        ),
        User(
            id=ids["bob"],
            tenant_id=ids["tenant_a"],
            external_subject="bob",
            name="Bob",
        ),
        User(
            id=ids["carol"],
            tenant_id=ids["tenant_b"],
            external_subject="carol",
            name="Carol",
        ),
        Department(
            id=ids["technology"],
            tenant_id=ids["tenant_a"],
            name="技术部",
        ),
        Department(
            id=ids["hr"],
            tenant_id=ids["tenant_a"],
            name="人力资源部",
        ),
        Department(
            id=ids["finance"],
            tenant_id=ids["tenant_b"],
            name="财务部",
        ),
        KnowledgeBase(
            id=ids["company_kb"],
            tenant_id=ids["tenant_a"],
            name="公司公共知识库",
            visibility="company",
        ),
        KnowledgeBase(
            id=ids["technology_kb"],
            tenant_id=ids["tenant_a"],
            name="技术部知识库",
            visibility="restricted",
        ),
        KnowledgeBase(
            id=ids["hr_kb"],
            tenant_id=ids["tenant_a"],
            name="人力资源部知识库",
            visibility="restricted",
        ),
        KnowledgeBase(
            id=ids["finance_kb"],
            tenant_id=ids["tenant_b"],
            name="财务知识库",
            visibility="restricted",
        ),
        KnowledgeBase(
            id=ids["special_kb"],
            tenant_id=ids["tenant_a"],
            name="专项知识库",
            visibility="restricted",
        ),
    ])

    session.flush()

    session.add_all([
        DepartmentMembership(
            tenant_id=ids["tenant_a"],
            department_id=ids["technology"],
            user_id=ids["alice"],
        ),
        DepartmentMembership(
            tenant_id=ids["tenant_a"],
            department_id=ids["hr"],
            user_id=ids["bob"],
        ),
        DepartmentMembership(
            tenant_id=ids["tenant_b"],
            department_id=ids["finance"],
            user_id=ids["carol"],
        ),
        KnowledgeBaseDepartmentGrant(
            tenant_id=ids["tenant_a"],
            knowledge_base_id=ids["technology_kb"],
            department_id=ids["technology"],
            permission="viewer",
        ),
        KnowledgeBaseDepartmentGrant(
            tenant_id=ids["tenant_a"],
            knowledge_base_id=ids["hr_kb"],
            department_id=ids["hr"],
            permission="viewer",
        ),
        KnowledgeBaseDepartmentGrant(
            tenant_id=ids["tenant_b"],
            knowledge_base_id=ids["finance_kb"],
            department_id=ids["finance"],
            permission="viewer",
        ),
        KnowledgeBaseUserGrant(
            tenant_id=ids["tenant_a"],
            knowledge_base_id=ids["special_kb"],
            user_id=ids["alice"],
            permission="editor",
        ),
    ])

    session.commit()

    return ids


def alice_principal(ids: dict) -> Principal:
    return Principal(
        user_id=ids["alice"],
        tenant_id=ids["tenant_a"],
        external_subject="alice",
    )


def test_company_knowledge_base_is_readable(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)

    assert service.can_access(
        principal=alice_principal(permission_data),
        knowledge_base_id=permission_data["company_kb"],
    )


def test_department_member_can_read_department_kb(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)

    assert service.can_access(
        principal=alice_principal(permission_data),
        knowledge_base_id=permission_data["technology_kb"],
    )


def test_other_department_is_denied(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)

    assert not service.can_access(
        principal=alice_principal(permission_data),
        knowledge_base_id=permission_data["hr_kb"],
    )


def test_cross_tenant_access_is_denied(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)

    assert not service.can_access(
        principal=alice_principal(permission_data),
        knowledge_base_id=permission_data["finance_kb"],
    )


def test_editor_permission_includes_viewer(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)
    principal = alice_principal(permission_data)

    assert service.can_access(
        principal=principal,
        knowledge_base_id=permission_data["special_kb"],
        required_permission="viewer",
    )

    assert service.can_access(
        principal=principal,
        knowledge_base_id=permission_data["special_kb"],
        required_permission="editor",
    )

    assert not service.can_access(
        principal=principal,
        knowledge_base_id=permission_data["special_kb"],
        required_permission="admin",
    )


def test_disabled_user_has_no_access(
    session: Session,
    permission_data: dict,
) -> None:
    alice = session.get(
        User,
        permission_data["alice"],
    )
    assert alice is not None

    alice.status = "disabled"
    session.commit()

    service = AuthorizationService(session)

    assert not service.can_access(
        principal=alice_principal(permission_data),
        knowledge_base_id=permission_data["company_kb"],
    )


def test_lists_only_readable_knowledge_bases(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)

    result = service.list_readable_knowledge_base_ids(
        principal=alice_principal(permission_data)
    )

    assert result == {
        permission_data["company_kb"],
        permission_data["technology_kb"],
        permission_data["special_kb"],
    }


def test_require_permission_raises_when_denied(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)

    with pytest.raises(AuthorizationDenied):
        service.require_permission(
            principal=alice_principal(permission_data),
            knowledge_base_id=permission_data["hr_kb"],
        )

def test_builds_scope_for_authorized_knowledge_base(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)
    principal = alice_principal(permission_data)

    scope = service.require_retrieval_scope(
        principal=principal,
        knowledge_base_id=(
            permission_data["technology_kb"]
        ),
    )

    assert scope.tenant_id == permission_data["tenant_a"]
    assert (
        scope.knowledge_base_id
        == permission_data["technology_kb"]
    )


def test_rejects_scope_for_unauthorized_knowledge_base(
    session: Session,
    permission_data: dict,
) -> None:
    service = AuthorizationService(session)
    principal = alice_principal(permission_data)

    with pytest.raises(AuthorizationDenied):
        service.require_retrieval_scope(
            principal=principal,
            knowledge_base_id=permission_data["hr_kb"],
        )
