import logging
from os import name as operating_system
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    DocumentIndexingJob,
    KnowledgeDocument,
)
from ..audit_service import (
    AuditService,
    log_committed_audit_event,
)
from ..security.retrieval_scope import RetrievalScope


logger = logging.getLogger(__name__)


class DocumentNotFoundError(LookupError):
    """指定范围内不存在该文档。"""


class DocumentDeletionService:
    """删除文档记录、向量分块和托管文件。"""

    def __init__(
        self,
        *,
        session: Session,
        storage_dir: str | Path,
    ) -> None:
        self._session = session
        self._storage_dir = (
            Path(storage_dir)
            .expanduser()
            .resolve()
        )

    def delete(
        self,
        *,
        scope: RetrievalScope,
        document_id: UUID,
        actor_user_id: UUID | None = None,
        request_id: str | None = None,
    ) -> None:
        document = self._session.scalar(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.id
                == document_id,
            )
            .with_for_update()
        )

        if document is None:
            self._session.rollback()
            raise DocumentNotFoundError(
                "文档不存在。"
            )

        managed_path = self._managed_file_path(
            document.storage_uri
        )
        queued_staged_paths: list[Path] = []

        for storage_uri in self._session.scalars(
            select(
                DocumentIndexingJob.staged_storage_uri
            ).where(
                DocumentIndexingJob.tenant_id
                == scope.tenant_id,
                DocumentIndexingJob.knowledge_base_id
                == scope.knowledge_base_id,
                DocumentIndexingJob.document_id
                == document_id,
            )
        ).all():
            queued_staged_path = (
                self._managed_staging_file_path(
                    storage_uri
                )
            )

            if queued_staged_path is not None:
                queued_staged_paths.append(
                    queued_staged_path
                )

        staged_path: Path | None = None

        if (
            managed_path is not None
            and managed_path.is_file()
        ):
            staged_path = managed_path.with_name(
                f".deleting-{uuid4().hex}.tmp"
            )

            managed_path.replace(staged_path)

        try:
            audit_event = None
            if actor_user_id is not None:
                audit_event = AuditService(
                    self._session
                ).add(
                    tenant_id=scope.tenant_id,
                    actor_user_id=actor_user_id,
                    knowledge_base_id=(
                        scope.knowledge_base_id
                    ),
                    action="document.deleted",
                    resource_type="document",
                    resource_id=document_id,
                    outcome="success",
                    request_id=request_id,
                    details={
                        "document_version": document.version,
                    },
                )
            self._session.delete(document)
            self._session.commit()
            if audit_event is not None:
                log_committed_audit_event(audit_event)

        except Exception:
            self._session.rollback()

            if (
                staged_path is not None
                and staged_path.exists()
            ):
                try:
                    if (
                        managed_path is not None
                        and not managed_path.exists()
                    ):
                        staged_path.replace(
                            managed_path
                        )

                except OSError as restore_error:
                    logger.exception(
                        "数据库删除失败，且暂存文件恢复失败：%s",
                        staged_path,
                    )
                    raise RuntimeError(
                        "删除失败，且原始文件恢复失败。"
                    ) from restore_error

            raise

        if (
            staged_path is not None
            and staged_path.exists()
        ):
            try:
                staged_path.unlink()

            except OSError:
                # 数据库删除已经提交，不能再向客户端报告失败。
                # 遗留的隐藏文件可以由后续清理任务处理。
                logger.exception(
                    "文档已删除，但暂存文件清理失败：%s",
                    staged_path,
                )

        for queued_staged_path in queued_staged_paths:
            try:
                queued_staged_path.unlink(
                    missing_ok=True
                )

            except OSError:
                # 数据库级联删除已经提交，遗留文件交给
                # 后续的孤儿暂存文件清理任务处理。
                logger.exception(
                    "索引任务已删除，但候选文件清理失败：%s",
                    queued_staged_path,
                )

            try:
                queued_staged_path.parent.rmdir()

            except OSError:
                # 其他文档可能仍有候选文件。
                pass

    def _managed_file_path(
        self,
        storage_uri: str,
    ) -> Path | None:
        """只返回文档存储目录中的本地文件路径。"""
        parsed = urlparse(storage_uri)

        if parsed.scheme.lower() != "file":
            return None

        if parsed.netloc not in {
            "",
            "localhost",
        }:
            return None

        decoded_path = unquote(parsed.path)

        if (
            operating_system == "nt"
            and len(decoded_path) >= 3
            and decoded_path[0] == "/"
            and decoded_path[2] == ":"
        ):
            # file:///D:/documents/file.txt
            # 转换为 D:/documents/file.txt。
            decoded_path = decoded_path[1:]

        local_path = Path(decoded_path).resolve()

        try:
            local_path.relative_to(
                self._storage_dir
            )

        except ValueError:
            # CLI 入库文件可能位于 sample_docs 等目录，
            # 删除索引时不能删除这些原始文件。
            return None

        return local_path

    def _managed_staging_file_path(
        self,
        storage_uri: str,
    ) -> Path | None:
        path = self._managed_file_path(storage_uri)

        if (
            path is None
            or path.parent.name != ".staging"
        ):
            return None

        return path
