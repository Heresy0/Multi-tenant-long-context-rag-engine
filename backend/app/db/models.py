from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
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
    text,
)
from sqlalchemy import event
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
EMBEDDING_DIMENSION = 1024
DEFAULT_TENANT_MAX_DOCUMENT_COUNT = 1000
DEFAULT_TENANT_MAX_STORAGE_BYTES = 10 * 1024 * 1024 * 1024

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

    max_document_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_TENANT_MAX_DOCUMENT_COUNT,
        server_default=text(
            str(DEFAULT_TENANT_MAX_DOCUMENT_COUNT)
        ),
    )

    max_storage_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=DEFAULT_TENANT_MAX_STORAGE_BYTES,
        server_default=text(
            str(DEFAULT_TENANT_MAX_STORAGE_BYTES)
        ),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_tenants_status",
        ),
        CheckConstraint(
            "max_document_count >= 1",
            name="ck_tenants_max_document_count",
        ),
        CheckConstraint(
            "max_storage_bytes >= 1",
            name="ck_tenants_max_storage_bytes",
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

    file_size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
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
        CheckConstraint(
            "file_size_bytes >= 0",
            name="ck_documents_file_size_bytes",
        ),
        Index(
            "ix_documents_tenant_kb_status",
            "tenant_id",
            "knowledge_base_id",
            "status",
        ),
    )


class DocumentIndexingJob(TimestampMixin, Base):
    """一次持久化的文档索引任务。"""

    __tablename__ = "document_indexing_jobs"

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

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    requested_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )

    staged_storage_uri: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    candidate_file_name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    candidate_mime_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    candidate_content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    candidate_size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    target_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="queued",
        server_default=text("'queued'"),
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    max_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default=text("3"),
    )

    indexed_chunk_count: Mapped[int | None] = (
        mapped_column(
            Integer,
            nullable=True,
        )
    )

    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    staged_candidate_deleted_at: Mapped[
        datetime | None
    ] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    reservation_released_at: Mapped[
        datetime | None
    ] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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
            name="fk_indexing_jobs_document",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "requested_by_user_id",
            ],
            [
                "users.tenant_id",
                "users.id",
            ],
            name="fk_indexing_jobs_requested_by_user",
        ),
        CheckConstraint(
            "status IN "
            "('queued', 'running', 'succeeded', 'failed')",
            name="ck_indexing_jobs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_indexing_jobs_attempt_count",
        ),
        CheckConstraint(
            "max_attempts >= 1",
            name="ck_indexing_jobs_max_attempts",
        ),
        CheckConstraint(
            "attempt_count <= max_attempts",
            name="ck_indexing_jobs_attempt_limit",
        ),
        CheckConstraint(
            "target_version >= 1",
            name="ck_indexing_jobs_target_version",
        ),
        CheckConstraint(
            "indexed_chunk_count IS NULL "
            "OR indexed_chunk_count >= 0",
            name="ck_indexing_jobs_chunk_count",
        ),
        CheckConstraint(
            "candidate_size_bytes >= 0",
            name="ck_indexing_jobs_candidate_size_bytes",
        ),
        Index(
            "ix_indexing_jobs_claim",
            "status",
            "available_at",
            "created_at",
        ),
        Index(
            "ix_indexing_jobs_scope_document",
            "tenant_id",
            "knowledge_base_id",
            "document_id",
            "created_at",
        ),
        Index(
            "ix_indexing_jobs_failed_cleanup",
            "status",
            "staged_candidate_deleted_at",
            "finished_at",
        ),
        Index(
            "ix_indexing_jobs_tenant_reservation",
            "tenant_id",
            "reservation_released_at",
        ),
        Index(
            "uq_indexing_jobs_active_document",
            "tenant_id",
            "knowledge_base_id",
            "document_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued', 'running')"
            ),
            sqlite_where=text(
                "status IN ('queued', 'running')"
            ),
        ),
    )


class IndexingWorkerHeartbeat(TimestampMixin, Base):
    """索引 Worker 实例的最近存活状态。"""

    __tablename__ = "indexing_worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="running",
        server_default=text("'running'"),
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'stopped')",
            name="ck_indexing_worker_heartbeats_status",
        ),
        Index(
            "ix_indexing_worker_heartbeats_status_seen",
            "status",
            "last_heartbeat_at",
        ),
    )


class AuditEvent(Base):
    """仅追加的安全审计事件。"""

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    knowledge_base_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    outcome: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    request_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    details_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'denied', 'invalid', 'failed')",
            name="ck_audit_events_outcome",
        ),
        Index(
            "ix_audit_events_tenant_occurred",
            "tenant_id",
            "occurred_at",
        ),
        Index(
            "ix_audit_events_actor_occurred",
            "tenant_id",
            "actor_user_id",
            "occurred_at",
        ),
        Index(
            "ix_audit_events_kb_occurred",
            "tenant_id",
            "knowledge_base_id",
            "occurred_at",
        ),
        Index(
            "ix_audit_events_action_occurred",
            "tenant_id",
            "action",
            "occurred_at",
        ),
    )


def _reject_audit_event_mutation(
    _mapper,
    _connection,
    _target: AuditEvent,
) -> None:
    raise ValueError(
        "审计事件是不可变记录，禁止更新或删除。"
    )


event.listen(
    AuditEvent,
    "before_update",
    _reject_audit_event_mutation,
)
event.listen(
    AuditEvent,
    "before_delete",
    _reject_audit_event_mutation,
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
