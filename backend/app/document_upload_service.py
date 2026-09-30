from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import KnowledgeDocument
from .document_staging_service import (
    DocumentStagingService,
    InvalidUpload,
    UnsupportedUploadType,
    UploadTooLarge,
)
from .pgvector_indexing_service import (
    PgVectorIndexingService,
)
from .security.retrieval_scope import RetrievalScope


@dataclass(frozen=True, slots=True)
class DocumentUploadResult:
    document: KnowledgeDocument
    indexed_chunk_count: int
    skipped: bool


class DocumentUploadService:
    """安全保存上传文件并写入 pgvector。"""

    def __init__(
        self,
        *,
        session: Session,
        indexing_service: PgVectorIndexingService,
        storage_dir: str | Path,
        max_upload_bytes: int,
    ) -> None:
        self._session = session
        self._indexing_service = indexing_service
        self._staging_service = DocumentStagingService(
            storage_dir=storage_dir,
            max_upload_bytes=max_upload_bytes,
        )

    def upload(
        self,
        *,
        file_name: str,
        source: BinaryIO,
        scope: RetrievalScope,
        created_by_user_id: UUID,
    ) -> DocumentUploadResult:
        staged_document = self._staging_service.stage(
            file_name=file_name,
            source=source,
            scope=scope,
        )

        directory = staged_document.target_path.parent
        target_path = staged_document.target_path
        temporary_path = staged_document.staged_path

        backup_path = (
            directory
            / f".backup-{uuid4().hex}.tmp"
        )

        backup_created = False
        target_installed = False

        try:
            if target_path.exists():
                target_path.replace(backup_path)
                backup_created = True

            temporary_path.replace(target_path)
            target_installed = True

            indexed_chunk_count = (
                self._indexing_service.index_file(
                    file_path=target_path,
                    scope=scope,
                    created_by_user_id=(
                        created_by_user_id
                    ),
                )
            )

        except Exception:
            if target_installed:
                target_path.unlink(
                    missing_ok=True
                )

            if backup_created:
                backup_path.replace(target_path)

            raise

        else:
            backup_path.unlink(
                missing_ok=True
            )

        finally:
            self._staging_service.discard(
                staged_document
            )

        document = self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.storage_uri
                == target_path.as_uri(),
            )
        )

        if document is None:
            raise RuntimeError(
                "入库完成但没有找到文档记录"
            )

        return DocumentUploadResult(
            document=document,
            indexed_chunk_count=(
                indexed_chunk_count
            ),
            skipped=indexed_chunk_count == 0,
        )
