from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.bootstrap import (
    BootstrapConflictError,
    bootstrap_initial_tenant,
    provision_department_manager,
)
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
    AuthorizationService,
)
from backend.app.security.principal import Principal


@pytest.fixture
def session() -> Iterator[Session]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as database_session:
        yield database_session

    Base.metadata.drop_all(engine)
    engine.dispose()


def bootstrap(session: Session):
    return bootstrap_initial_tenant(
        session,
        tenant_name="示例企业",
        department_name="技术部",
        external_subject="keycloak-alice",
        user_name="Alice",
        user_email="alice@example.local",
    )


def count(session: Session, model) -> int:
    return session.scalar(
        select(func.count()).select_from(model)
    )


def test_creates_minimum_enterprise_data(
    session: Session,
) -> None:
    result = bootstrap(session)

    assert count(session, Tenant) == 1
    assert count(session, User) == 1
    assert count(session, Department) == 1
    assert count(session, DepartmentMembership) == 1
    assert count(session, KnowledgeBase) == 2
    assert count(session, KnowledgeBaseDepartmentGrant) == 1
    assert count(session, KnowledgeBaseUserGrant) == 2

    principal = Principal(
        user_id=result.user_id,
        tenant_id=result.tenant_id,
        external_subject="keycloak-alice",
    )
    authorization = AuthorizationService(session)

    assert authorization.get_permission(
        principal=principal,
        knowledge_base_id=(
            result.company_knowledge_base_id
        ),
    ) == "admin"
    assert authorization.get_permission(
        principal=principal,
        knowledge_base_id=(
            result.department_knowledge_base_id
        ),
    ) == "admin"


def test_is_idempotent(session: Session) -> None:
    first = bootstrap(session)
    second = bootstrap(session)

    assert second == first
    assert count(session, Tenant) == 1
    assert count(session, User) == 1
    assert count(session, Department) == 1
    assert count(session, KnowledgeBase) == 2


def test_rejects_subject_bound_to_another_tenant(
    session: Session,
) -> None:
    bootstrap(session)

    with pytest.raises(
        BootstrapConflictError,
        match="已绑定到其他租户",
    ):
        bootstrap_initial_tenant(
            session,
            tenant_name="另一个企业",
            department_name="财务部",
            external_subject="keycloak-alice",
            user_name="Alice",
        )

    assert count(session, Tenant) == 1


def test_provisions_department_manager_with_isolation(
    session: Session,
) -> None:
    alice = bootstrap(session)

    bob = provision_department_manager(
        session,
        tenant_name="示例企业",
        department_name="人力资源部",
        external_subject="keycloak-bob",
        user_name="Bob",
        user_email="bob@example.local",
    )

    assert bob.tenant_id == alice.tenant_id
    assert count(session, Tenant) == 1
    assert count(session, User) == 2
    assert count(session, Department) == 2
    assert count(session, KnowledgeBase) == 3

    authorization = AuthorizationService(session)
    alice_principal = Principal(
        user_id=alice.user_id,
        tenant_id=alice.tenant_id,
        external_subject="keycloak-alice",
    )
    bob_principal = Principal(
        user_id=bob.user_id,
        tenant_id=bob.tenant_id,
        external_subject="keycloak-bob",
    )

    assert authorization.get_permission(
        principal=alice_principal,
        knowledge_base_id=(
            bob.department_knowledge_base_id
        ),
    ) is None
    assert authorization.get_permission(
        principal=bob_principal,
        knowledge_base_id=(
            alice.department_knowledge_base_id
        ),
    ) is None
    assert authorization.get_permission(
        principal=bob_principal,
        knowledge_base_id=bob.company_knowledge_base_id,
    ) == "viewer"
    assert authorization.get_permission(
        principal=bob_principal,
        knowledge_base_id=(
            bob.department_knowledge_base_id
        ),
    ) == "admin"


def test_department_manager_provision_is_idempotent(
    session: Session,
) -> None:
    bootstrap(session)
    arguments = {
        "tenant_name": "示例企业",
        "department_name": "人力资源部",
        "external_subject": "keycloak-bob",
        "user_name": "Bob",
    }

    first = provision_department_manager(
        session,
        **arguments,
    )
    second = provision_department_manager(
        session,
        **arguments,
    )

    assert second == first
    assert count(session, User) == 2
    assert count(session, Department) == 2
    assert count(session, KnowledgeBase) == 3
