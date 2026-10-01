import logging

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import (
    DocumentIndexingJob,
    KnowledgeDocument,
)
from .audit_service import (
    AuditService,
    log_committed_audit_event,
)
from .document_indexing_job_service import (
    DocumentIndexingJobService,
)
from .document_staging_service import (
    DocumentStagingService,
    InvalidUpload,
    UnsupportedUploadType,
    UploadTooLarge,
)
from .rag import calculate_source_id
from .metrics import (
    record_tenant_upload_quota_rejection,
)
from .security.retrieval_scope import RetrievalScope
from .tenant_document_quota_service import (
    TenantDocumentQuotaExceeded,
    TenantDocumentQuotaService,
)


logger = logging.getLogger(__name__)


class DocumentUploadConflict(RuntimeError):
    """同一文档正在创建或处理上传任务。"""


@dataclass(frozen=True, slots=True)
class DocumentUploadResult:
    document: KnowledgeDocument
    indexing_job: DocumentIndexingJob


class DocumentUploadService:
    """安全暂存上传文件并创建持久化索引任务。"""

    def __init__(
        self,
        *,
        session: Session,
        storage_dir: str | Path,
        max_upload_bytes: int,
    ) -> None:
        self._session = session
        self._staging_service = DocumentStagingService(
            storage_dir=storage_dir,
            max_upload_bytes=max_upload_bytes,
        )
        self._job_service = DocumentIndexingJobService(
            session=session
        )
        self._quota_service = TenantDocumentQuotaService(
            session
        )

    def upload(
        self,
        *,
        file_name: str,
        source: BinaryIO,
        scope: RetrievalScope,
        created_by_user_id: UUID,
        request_id: str | None = None,
    ) -> DocumentUploadResult:
        staged_document = self._staging_service.stage(
            file_name=file_name,
            source=source,
            scope=scope,
        )
        source_id = calculate_source_id(
            staged_document.target_path
        )
        final_storage_uri = (
            staged_document.target_path.as_uri()
        )

        try:
            tenant = self._quota_service.lock_tenant(
                scope.tenant_id
            )
            document = self._session.scalar(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id
                    == scope.tenant_id,
                    KnowledgeDocument.knowledge_base_id
                    == scope.knowledge_base_id,
                    KnowledgeDocument.source_id
                    == source_id,
                )
                .with_for_update()
            )

            self._quota_service.require_capacity(
                tenant=tenant,
                candidate_size_bytes=(
                    staged_document.size_bytes
                ),
                creates_document=document is None,
            )

            if document is None:
                document = KnowledgeDocument(
                    tenant_id=scope.tenant_id,
                    knowledge_base_id=(
                        scope.knowledge_base_id
                    ),
                    created_by_user_id=(
                        created_by_user_id
                    ),
                    source_id=source_id,
                    file_name=(
                        staged_document.file_name
                    ),
                    storage_uri=final_storage_uri,
                    mime_type=staged_document.mime_type,
                    content_hash=(
                        staged_document.content_hash
                    ),
                    status="pending",
                    version=1,
                    metadata_json={},
                )
                self._session.add(document)
                self._session.flush()
                target_version = 1

            elif document.status == "ready":
                candidate_is_unchanged = (
                    document.file_name
                    == staged_document.file_name
                    and document.storage_uri
                    == final_storage_uri
                    and document.mime_type
                    == staged_document.mime_type
                    and document.content_hash
                    == staged_document.content_hash
                )
                target_version = (
                    document.version
                    if candidate_is_unchanged
                    else document.version + 1
                )

            else:
                # 首次索引失败后仍然复用初始版本；此时没有
                # 可供检索的旧版本需要保留。
                document.created_by_user_id = (
                    created_by_user_id
                )
                document.file_name = (
                    staged_document.file_name
                )
                document.storage_uri = final_storage_uri
                document.mime_type = (
                    staged_document.mime_type
                )
                document.content_hash = (
                    staged_document.content_hash
                )
                document.status = "pending"
                document.last_error = None
                target_version = document.version
                self._session.flush()

            indexing_job = self._job_service.create(
                scope=scope,
                document_id=document.id,
                requested_by_user_id=(
                    created_by_user_id
                ),
                staged_storage_uri=(
                    staged_document.staged_path.as_uri()
                ),
                candidate_file_name=(
                    staged_document.file_name
                ),
                candidate_mime_type=(
                    staged_document.mime_type
                ),
                candidate_content_hash=(
                    staged_document.content_hash
                ),
                candidate_size_bytes=(
                    staged_document.size_bytes
                ),
                target_version=target_version,
                commit=False,
            )
            audit_event = AuditService(
                self._session
            ).add(
                tenant_id=scope.tenant_id,
                actor_user_id=created_by_user_id,
                knowledge_base_id=(
                    scope.knowledge_base_id
                ),
                action="document.upload_requested",
                resource_type="document",
                resource_id=document.id,
                outcome="success",
                request_id=request_id,
                details={
                    "indexing_job_id": str(
                        indexing_job.id
                    ),
                    "target_version": target_version,
                },
            )
            self._session.commit()
            self._session.refresh(document)
            self._session.refresh(indexing_job)
            log_committed_audit_event(audit_event)

        except TenantDocumentQuotaExceeded as exc:
            self._session.rollback()
            self._staging_service.discard(
                staged_document
            )
            record_tenant_upload_quota_rejection(
                exc.resource
            )
            try:
                AuditService(self._session).record(
                    tenant_id=scope.tenant_id,
                    actor_user_id=created_by_user_id,
                    knowledge_base_id=(
                        scope.knowledge_base_id
                    ),
                    action="document.upload_requested",
                    resource_type="tenant_quota",
                    outcome="denied",
                    request_id=request_id,
                    details={
                        "reason": exc.resource,
                        "limit": exc.limit,
                        "used": exc.used,
                        "requested": exc.requested,
                    },
                )
            except Exception:
                logger.exception(
                    "租户上传配额拒绝审计写入失败"
                )
            raise

        except IntegrityError as exc:
            self._session.rollback()
            self._staging_service.discard(
                staged_document
            )
            raise DocumentUploadConflict(
                "同名文档正在上传，请稍后重试。"
            ) from exc

        except Exception:
            self._session.rollback()
            self._staging_service.discard(
                staged_document
            )
            raise

        return DocumentUploadResult(
            document=document,
            indexing_job=indexing_job,
        )
