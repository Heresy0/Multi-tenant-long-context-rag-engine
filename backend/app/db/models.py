from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TimestampMixin:
    """统一记录数据的创建和更新时间。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_tenants_status",
        ),
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "tenants.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    external_subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    email: Mapped[str | None] = mapped_column(
        String(320),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "external_subject",
            name="uq_users_tenant_external_subject",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_users_tenant_id",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_users_status",
        ),
        Index(
            "ix_users_tenant_status",
            "tenant_id",
            "status",
        ),
    )


class Department(TimestampMixin, Base):
    __tablename__ = "departments"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "tenants.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "name",
            name="uq_departments_tenant_name",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_departments_tenant_id",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_departments_status",
        ),
    )


class DepartmentMembership(TimestampMixin, Base):
    __tablename__ = "department_memberships"

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="member",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "department_id"],
            ["departments.tenant_id", "departments.id"],
            name="fk_memberships_department",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_memberships_user",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "role IN ('member', 'manager')",
            name="ck_department_memberships_role",
        ),
        Index(
            "ix_memberships_tenant_user",
            "tenant_id",
            "user_id",
        ),
    )


class KnowledgeBase(TimestampMixin, Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "tenants.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    visibility: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="restricted",
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "name",
            name="uq_knowledge_bases_tenant_name",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_knowledge_bases_tenant_id",
        ),
        CheckConstraint(
            "visibility IN ('company', 'restricted')",
            name="ck_knowledge_bases_visibility",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_knowledge_bases_status",
        ),
        Index(
            "ix_knowledge_bases_tenant_status",
            "tenant_id",
            "status",
        ),
    )


class KnowledgeBaseDepartmentGrant(
    TimestampMixin,
    Base,
):
    __tablename__ = "knowledge_base_department_grants"

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    department_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    permission: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="viewer",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "knowledge_base_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_kb_department_grants_kb",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "department_id"],
            ["departments.tenant_id", "departments.id"],
            name="fk_kb_department_grants_department",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "permission IN ('viewer', 'editor', 'admin')",
            name="ck_kb_department_grants_permission",
        ),
        Index(
            "ix_kb_department_grants_department",
            "tenant_id",
            "department_id",
        ),
    )


class KnowledgeBaseUserGrant(
    TimestampMixin,
    Base,
):
    __tablename__ = "knowledge_base_user_grants"

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )

    permission: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="viewer",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "knowledge_base_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_kb_user_grants_kb",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_kb_user_grants_user",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "permission IN ('viewer', 'editor', 'admin')",
            name="ck_kb_user_grants_permission",
        ),
        Index(
            "ix_kb_user_grants_user",
            "tenant_id",
            "user_id",
        ),
    )