from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..db.models import (
    Department,
    DepartmentMembership,
    KnowledgeBase,
    KnowledgeBaseDepartmentGrant,
    KnowledgeBaseUserGrant,
    Tenant,
    User,
)
from .principal import Principal


PERMISSION_LEVELS = {
    "viewer": 1,
    "editor": 2,
    "admin": 3,
}


class AuthorizationDenied(PermissionError):
    """当前用户无权执行请求的操作。"""


class AuthorizationService:
    def __init__(
        self,
        session: Session,
    ) -> None:
        self._session = session

    def get_permission(
        self,
        *,
        principal: Principal,
        knowledge_base_id: UUID,
    ) -> str | None:
        """返回用户对指定知识库拥有的最高权限。"""
        if not self._is_active_principal(principal):
            return None

        knowledge_base = self._session.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.tenant_id
                == principal.tenant_id,
                KnowledgeBase.status == "active",
            )
        )

        if knowledge_base is None:
            return None

        permissions: list[str] = []

        if knowledge_base.visibility == "company":
            permissions.append("viewer")

        direct_permissions = self._session.scalars(
            select(
                KnowledgeBaseUserGrant.permission
            ).where(
                KnowledgeBaseUserGrant.tenant_id
                == principal.tenant_id,
                KnowledgeBaseUserGrant.knowledge_base_id
                == knowledge_base_id,
                KnowledgeBaseUserGrant.user_id
                == principal.user_id,
            )
        ).all()

        permissions.extend(direct_permissions)

        department_permissions = self._session.scalars(
            select(
                KnowledgeBaseDepartmentGrant.permission
            )
            .join(
                DepartmentMembership,
                and_(
                    DepartmentMembership.tenant_id
                    == KnowledgeBaseDepartmentGrant.tenant_id,
                    DepartmentMembership.department_id
                    == KnowledgeBaseDepartmentGrant.department_id,
                ),
            )
            .join(
                Department,
                and_(
                    Department.tenant_id
                    == DepartmentMembership.tenant_id,
                    Department.id
                    == DepartmentMembership.department_id,
                ),
            )
            .where(
                KnowledgeBaseDepartmentGrant.tenant_id
                == principal.tenant_id,
                KnowledgeBaseDepartmentGrant.knowledge_base_id
                == knowledge_base_id,
                DepartmentMembership.user_id
                == principal.user_id,
                Department.status == "active",
            )
        ).all()

        permissions.extend(department_permissions)

        if not permissions:
            return None

        return max(
            permissions,
            key=PERMISSION_LEVELS.__getitem__,
        )

    def can_access(
        self,
        *,
        principal: Principal,
        knowledge_base_id: UUID,
        required_permission: str = "viewer",
    ) -> bool:
        """判断用户是否具备要求的权限。"""
        required_level = PERMISSION_LEVELS.get(
            required_permission
        )

        if required_level is None:
            raise ValueError(
                f"未知权限：{required_permission}"
            )

        actual_permission = self.get_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
        )

        if actual_permission is None:
            return False

        return (
            PERMISSION_LEVELS[actual_permission]
            >= required_level
        )

    def require_permission(
        self,
        *,
        principal: Principal,
        knowledge_base_id: UUID,
        required_permission: str = "viewer",
    ) -> None:
        """无权限时抛出统一异常。"""
        if not self.can_access(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission=required_permission,
        ):
            raise AuthorizationDenied(
                "没有权限访问该知识库。"
            )

    def list_readable_knowledge_base_ids(
        self,
        *,
        principal: Principal,
    ) -> set[UUID]:
        """列出当前用户可以查询的知识库。"""
        if not self._is_active_principal(principal):
            return set()

        knowledge_base_ids = self._session.scalars(
            select(KnowledgeBase.id).where(
                KnowledgeBase.tenant_id
                == principal.tenant_id,
                KnowledgeBase.status == "active",
            )
        ).all()

        return {
            knowledge_base_id
            for knowledge_base_id
            in knowledge_base_ids
            if self.can_access(
                principal=principal,
                knowledge_base_id=knowledge_base_id,
            )
        }

    def _is_active_principal(
        self,
        principal: Principal,
    ) -> bool:
        user_id = self._session.scalar(
            select(User.id)
            .join(
                Tenant,
                Tenant.id == User.tenant_id,
            )
            .where(
                User.id == principal.user_id,
                User.tenant_id
                == principal.tenant_id,
                User.external_subject
                == principal.external_subject,
                User.status == "active",
                Tenant.status == "active",
            )
        )

        return user_id is not None