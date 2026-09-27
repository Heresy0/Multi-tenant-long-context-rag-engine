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
    Integer,
    JSON,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
EMBEDDING_DIMENSION = 1024

from pgvector.sqlalchemy import VECTOR


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


class KnowledgeDocument(TimestampMixin, Base):
    """知识库中的原始文档记录。"""

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )

    source_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    file_name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    storage_uri: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "knowledge_base_id"],
            [
                "knowledge_bases.tenant_id",
                "knowledge_bases.id",
            ],
            name="fk_documents_knowledge_base",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_documents_created_by_user",
        ),
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "id",
            name="uq_documents_tenant_kb_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "source_id",
            name="uq_documents_tenant_kb_source",
        ),
        CheckConstraint(
            "status IN "
            "('pending', 'indexing', 'ready', 'failed')",
            name="ck_documents_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_documents_version",
        ),
        Index(
            "ix_documents_tenant_kb_status",
            "tenant_id",
            "knowledge_base_id",
            "status",
        ),
    )


class DocumentChunk(TimestampMixin, Base):
    """文档切分后用于混合检索的内容块。"""

    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    knowledge_base_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    parent_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    chunking_version: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    embedding_model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    embedding: Mapped[list[float]] = mapped_column(
        VECTOR(EMBEDDING_DIMENSION),
        nullable=False,
    )

    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "knowledge_base_id",
                "document_id",
            ],
            [
                "documents.tenant_id",
                "documents.knowledge_base_id",
                "documents.id",
            ],
            name="fk_document_chunks_document",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_id",
            "document_id",
            "chunk_index",
            name="uq_document_chunks_position",
        ),
        CheckConstraint(
            "chunk_index >= 0",
            name="ck_document_chunks_index",
        ),
        Index(
            "ix_document_chunks_scope_document",
            "tenant_id",
            "knowledge_base_id",
            "document_id",
        ),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={
                "m": 16,
                "ef_construction": 64,
            },
            postgresql_ops={
                "embedding": "vector_cosine_ops",
            },
        ),
    )