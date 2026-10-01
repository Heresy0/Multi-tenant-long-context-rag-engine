from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import (
    DocumentIndexingJob,
    KnowledgeDocument,
    Tenant,
)


class TenantNotFoundError(LookupError):
    """租户不存在。"""


@dataclass(frozen=True, slots=True)
class TenantQuotaSnapshot:
    document_count: int
    stored_bytes: int
    reserved_bytes: int
    max_document_count: int
    max_storage_bytes: int

    @property
    def used_storage_bytes(self) -> int:
        return self.stored_bytes + self.reserved_bytes


class TenantDocumentQuotaExceeded(RuntimeError):
    """租户文档数量或存储空间超过配额。"""

    def __init__(
        self,
        message: str,
        *,
        resource: str,
        limit: int,
        used: int,
        requested: int,
        snapshot: TenantQuotaSnapshot,
    ) -> None:
        super().__init__(message)
        self.resource = resource
        self.limit = limit
        self.used = used
        self.requested = requested
        self.snapshot = snapshot


class TenantDocumentQuotaService:
    """在租户行锁保护下计算并校验文档上传配额。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_tenant(self, tenant_id: UUID) -> Tenant:
        tenant = self._session.scalar(
            select(Tenant)
            .where(Tenant.id == tenant_id)
            .with_for_update()
        )
        if tenant is None:
            raise TenantNotFoundError("租户不存在。")
        return tenant

    def snapshot(
        self,
        *,
        tenant: Tenant,
    ) -> TenantQuotaSnapshot:
        document_count = self._session.scalar(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == tenant.id
            )
        )
        stored_bytes = self._session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        KnowledgeDocument.file_size_bytes
                    ),
                    0,
                )
            ).where(
                KnowledgeDocument.tenant_id == tenant.id
            )
        )
        reserved_bytes = self._session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        DocumentIndexingJob
                        .candidate_size_bytes
                    ),
                    0,
                )
            ).where(
                DocumentIndexingJob.tenant_id == tenant.id,
                DocumentIndexingJob
                .reservation_released_at
                .is_(None),
            )
        )
        return TenantQuotaSnapshot(
            document_count=int(document_count or 0),
            stored_bytes=int(stored_bytes or 0),
            reserved_bytes=int(reserved_bytes or 0),
            max_document_count=tenant.max_document_count,
            max_storage_bytes=tenant.max_storage_bytes,
        )

    def require_capacity(
        self,
        *,
        tenant: Tenant,
        candidate_size_bytes: int,
        creates_document: bool,
    ) -> TenantQuotaSnapshot:
        if candidate_size_bytes < 0:
            raise ValueError(
                "candidate_size_bytes 不能小于 0"
            )

        snapshot = self.snapshot(tenant=tenant)
        additional_documents = 1 if creates_document else 0

        if (
            snapshot.document_count + additional_documents
            > snapshot.max_document_count
        ):
            raise TenantDocumentQuotaExceeded(
                "租户文档数量已达到配额上限。",
                resource="documents",
                limit=snapshot.max_document_count,
                used=snapshot.document_count,
                requested=additional_documents,
                snapshot=snapshot,
            )

        if (
            snapshot.used_storage_bytes
            + candidate_size_bytes
            > snapshot.max_storage_bytes
        ):
            raise TenantDocumentQuotaExceeded(
                "租户文档存储空间不足。",
                resource="storage_bytes",
                limit=snapshot.max_storage_bytes,
                used=snapshot.used_storage_bytes,
                requested=candidate_size_bytes,
                snapshot=snapshot,
            )

        return snapshot
