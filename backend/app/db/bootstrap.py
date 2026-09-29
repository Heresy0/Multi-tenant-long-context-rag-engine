from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Department,
    DepartmentMembership,
    KnowledgeBase,
    KnowledgeBaseDepartmentGrant,
    KnowledgeBaseUserGrant,
    Tenant,
    User,
)


class BootstrapConflictError(RuntimeError):
    """初始化参数与已有企业数据冲突。"""


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    tenant_id: UUID
    user_id: UUID
    department_id: UUID
    company_knowledge_base_id: UUID
    department_knowledge_base_id: UUID


@dataclass(frozen=True, slots=True)
class DepartmentManagerProvisionResult:
    tenant_id: UUID
    user_id: UUID
    department_id: UUID
    company_knowledge_base_id: UUID
    department_knowledge_base_id: UUID


def bootstrap_initial_tenant(
    session: Session,
    *,
    tenant_name: str,
    department_name: str,
    external_subject: str,
    user_name: str,
    user_email: str | None = None,
    company_knowledge_base_name: str = "公司公共知识库",
    department_knowledge_base_name: str | None = None,
) -> BootstrapResult:
    """幂等创建首个企业及其管理员的最小权限数据。"""
    values = {
        "企业名称": tenant_name,
        "部门名称": department_name,
        "Keycloak sub": external_subject,
        "用户名称": user_name,
        "公共知识库名称": company_knowledge_base_name,
    }

    for label, value in values.items():
        if not value.strip():
            raise ValueError(f"{label}不能为空。")

    tenant_name = tenant_name.strip()
    department_name = department_name.strip()
    external_subject = external_subject.strip()
    user_name = user_name.strip()
    user_email = user_email.strip() if user_email else None
    company_knowledge_base_name = (
        company_knowledge_base_name.strip()
    )
    department_knowledge_base_name = (
        department_knowledge_base_name.strip()
        if department_knowledge_base_name
        else f"{department_name}知识库"
    )

    try:
        tenant = _get_single_tenant_by_name(
            session,
            tenant_name,
        )

        if tenant is None:
            tenant = Tenant(name=tenant_name)
            session.add(tenant)
            session.flush()

        user = _get_single_user_by_subject(
            session,
            external_subject,
        )

        if user is not None and user.tenant_id != tenant.id:
            raise BootstrapConflictError(
                "该 Keycloak sub 已绑定到其他租户。"
            )

        if user is None:
            user = User(
                tenant_id=tenant.id,
                external_subject=external_subject,
                name=user_name,
                email=user_email,
            )
            session.add(user)
            session.flush()

        department = session.scalar(
            select(Department).where(
                Department.tenant_id == tenant.id,
                Department.name == department_name,
            )
        )

        if department is None:
            department = Department(
                tenant_id=tenant.id,
                name=department_name,
            )
            session.add(department)
            session.flush()

        company_knowledge_base = _get_or_create_knowledge_base(
            session,
            tenant_id=tenant.id,
            name=company_knowledge_base_name,
            visibility="company",
        )
        department_knowledge_base = _get_or_create_knowledge_base(
            session,
            tenant_id=tenant.id,
            name=department_knowledge_base_name,
            visibility="restricted",
        )

        _ensure_membership(
            session,
            tenant_id=tenant.id,
            department_id=department.id,
            user_id=user.id,
            role="manager",
        )
        _ensure_department_grant(
            session,
            tenant_id=tenant.id,
            knowledge_base_id=department_knowledge_base.id,
            department_id=department.id,
            permission="viewer",
        )

        for knowledge_base in (
            company_knowledge_base,
            department_knowledge_base,
        ):
            _ensure_user_grant(
                session,
                tenant_id=tenant.id,
                knowledge_base_id=knowledge_base.id,
                user_id=user.id,
                permission="admin",
            )

        session.commit()

    except Exception:
        session.rollback()
        raise

    return BootstrapResult(
        tenant_id=tenant.id,
        user_id=user.id,
        department_id=department.id,
        company_knowledge_base_id=company_knowledge_base.id,
        department_knowledge_base_id=(
            department_knowledge_base.id
        ),
    )


def provision_department_manager(
    session: Session,
    *,
    tenant_name: str,
    department_name: str,
    external_subject: str,
    user_name: str,
    user_email: str | None = None,
    company_knowledge_base_name: str = "公司公共知识库",
    department_knowledge_base_name: str | None = None,
) -> DepartmentManagerProvisionResult:
    """幂等开通已有企业的部门负责人及部门知识库。"""
    values = {
        "企业名称": tenant_name,
        "部门名称": department_name,
        "Keycloak sub": external_subject,
        "用户名称": user_name,
        "公共知识库名称": company_knowledge_base_name,
    }

    for label, value in values.items():
        if not value.strip():
            raise ValueError(f"{label}不能为空。")

    tenant_name = tenant_name.strip()
    department_name = department_name.strip()
    external_subject = external_subject.strip()
    user_name = user_name.strip()
    user_email = user_email.strip() if user_email else None
    company_knowledge_base_name = (
        company_knowledge_base_name.strip()
    )
    department_knowledge_base_name = (
        department_knowledge_base_name.strip()
        if department_knowledge_base_name
        else f"{department_name}知识库"
    )

    try:
        tenant = _get_single_tenant_by_name(
            session,
            tenant_name,
        )

        if tenant is None:
            raise BootstrapConflictError(
                f"企业不存在：{tenant_name}。"
            )

        user = _get_single_user_by_subject(
            session,
            external_subject,
        )

        if user is not None and user.tenant_id != tenant.id:
            raise BootstrapConflictError(
                "该 Keycloak sub 已绑定到其他租户。"
            )

        if user is None:
            user = User(
                tenant_id=tenant.id,
                external_subject=external_subject,
                name=user_name,
                email=user_email,
            )
            session.add(user)
            session.flush()

        department = session.scalar(
            select(Department).where(
                Department.tenant_id == tenant.id,
                Department.name == department_name,
            )
        )

        if department is None:
            department = Department(
                tenant_id=tenant.id,
                name=department_name,
            )
            session.add(department)
            session.flush()

        company_knowledge_base = _get_or_create_knowledge_base(
            session,
            tenant_id=tenant.id,
            name=company_knowledge_base_name,
            visibility="company",
        )
        department_knowledge_base = _get_or_create_knowledge_base(
            session,
            tenant_id=tenant.id,
            name=department_knowledge_base_name,
            visibility="restricted",
        )

        _ensure_membership(
            session,
            tenant_id=tenant.id,
            department_id=department.id,
            user_id=user.id,
            role="manager",
        )
        _ensure_department_grant(
            session,
            tenant_id=tenant.id,
            knowledge_base_id=department_knowledge_base.id,
            department_id=department.id,
            permission="viewer",
        )
        _ensure_user_grant(
            session,
            tenant_id=tenant.id,
            knowledge_base_id=department_knowledge_base.id,
            user_id=user.id,
            permission="admin",
        )

        session.commit()

    except Exception:
        session.rollback()
        raise

    return DepartmentManagerProvisionResult(
        tenant_id=tenant.id,
        user_id=user.id,
        department_id=department.id,
        company_knowledge_base_id=company_knowledge_base.id,
        department_knowledge_base_id=(
            department_knowledge_base.id
        ),
    )


def _get_single_tenant_by_name(
    session: Session,
    name: str,
) -> Tenant | None:
    tenants = session.scalars(
        select(Tenant).where(Tenant.name == name)
    ).all()

    if len(tenants) > 1:
        raise BootstrapConflictError(
            f"存在多个同名租户：{name}。"
        )

    return tenants[0] if tenants else None


def _get_single_user_by_subject(
    session: Session,
    external_subject: str,
) -> User | None:
    users = session.scalars(
        select(User).where(
            User.external_subject == external_subject
        )
    ).all()

    if len(users) > 1:
        raise BootstrapConflictError(
            "该 Keycloak sub 已绑定到多个本地用户。"
        )

    return users[0] if users else None


def _get_or_create_knowledge_base(
    session: Session,
    *,
    tenant_id: UUID,
    name: str,
    visibility: str,
) -> KnowledgeBase:
    knowledge_base = session.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.name == name,
        )
    )

    if knowledge_base is None:
        knowledge_base = KnowledgeBase(
            tenant_id=tenant_id,
            name=name,
            visibility=visibility,
        )
        session.add(knowledge_base)
        session.flush()

    elif knowledge_base.visibility != visibility:
        raise BootstrapConflictError(
            f"知识库“{name}”的可见性与初始化参数冲突。"
        )

    return knowledge_base


def _ensure_membership(
    session: Session,
    *,
    tenant_id: UUID,
    department_id: UUID,
    user_id: UUID,
    role: str,
) -> None:
    membership = session.get(
        DepartmentMembership,
        (tenant_id, department_id, user_id),
    )

    if membership is None:
        session.add(
            DepartmentMembership(
                tenant_id=tenant_id,
                department_id=department_id,
                user_id=user_id,
                role=role,
            )
        )
    else:
        membership.role = role


def _ensure_department_grant(
    session: Session,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    department_id: UUID,
    permission: str,
) -> None:
    grant = session.get(
        KnowledgeBaseDepartmentGrant,
        (tenant_id, knowledge_base_id, department_id),
    )

    if grant is None:
        session.add(
            KnowledgeBaseDepartmentGrant(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                department_id=department_id,
                permission=permission,
            )
        )
    else:
        grant.permission = permission


def _ensure_user_grant(
    session: Session,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    user_id: UUID,
    permission: str,
) -> None:
    grant = session.get(
        KnowledgeBaseUserGrant,
        (tenant_id, knowledge_base_id, user_id),
    )

    if grant is None:
        session.add(
            KnowledgeBaseUserGrant(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                user_id=user_id,
                permission=permission,
            )
        )
    else:
        grant.permission = permission
