import logging
from datetime import datetime, timedelta, timezone
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db.models import (
    DocumentIndexingJob,
    KnowledgeDocument,
)
from .document_indexing_job_service import (
    DocumentIndexingJobService,
)
from .managed_document_files import (
    CandidateFileSwap,
    ManagedDocumentFileService,
)
from .observability import event_message
from .metrics import record_indexing_job
from .pgvector_indexing_service import (
    PgVectorIndexingService,
)
from .security.retrieval_scope import RetrievalScope


logger = logging.getLogger(__name__)


class DocumentIndexingWorker:
    """领取并执行一条持久化文档索引任务。"""

    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        indexing_service: PgVectorIndexingService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._job_service = DocumentIndexingJobService(
            session=session,
            retry_delay_seconds=(
                settings.indexing_job_retry_delay_seconds
            ),
        )
        self._indexing_service = (
            indexing_service
            or PgVectorIndexingService(
                session=session,
                settings=settings,
            )
        )
        self._file_service = ManagedDocumentFileService(
            storage_dir=settings.document_storage_dir
        )

    def run_once(self) -> bool:
        """处理一条任务；没有可执行任务时返回 False。"""
        job = self._job_service.claim_next()

        if job is None:
            return False

        started = perf_counter()

        scope = RetrievalScope(
            tenant_id=job.tenant_id,
            knowledge_base_id=job.knowledge_base_id,
        )
        swap: CandidateFileSwap | None = None

        logger.info(
            event_message(
                "indexing.job.started",
                job_id=str(job.id),
                document_id=str(job.document_id),
                tenant_id=str(job.tenant_id),
                knowledge_base_id=str(
                    job.knowledge_base_id
                ),
                attempt_count=job.attempt_count,
            )
        )

        try:
            document = self._get_document(
                job=job,
                scope=scope,
            )
            self._mark_document_indexing(document)

            swap = self._file_service.prepare_candidate(
                job_id=job.id,
                scope=scope,
                staged_storage_uri=(
                    job.staged_storage_uri
                ),
                target_storage_uri=(
                    document.storage_uri
                ),
                candidate_content_hash=(
                    job.candidate_content_hash
                ),
            )

            indexed_chunk_count = (
                self._indexing_service
                .index_document_candidate(
                    file_path=swap.target_path,
                    scope=scope,
                    document_id=document.id,
                    created_by_user_id=(
                        job.requested_by_user_id
                    ),
                    target_version=job.target_version,
                    candidate_file_name=(
                        job.candidate_file_name
                    ),
                    candidate_mime_type=(
                        job.candidate_mime_type
                    ),
                    candidate_content_hash=(
                        job.candidate_content_hash
                    ),
                    final_storage_uri=(
                        document.storage_uri
                    ),
                )
            )

        except Exception as exc:
            self._handle_failure(
                job=job,
                scope=scope,
                swap=swap,
                error=exc,
                duration_ms=round(
                    (perf_counter() - started) * 1000,
                    2,
                ),
            )
            return True

        # 索引服务已经提交文档与分块。之后即使任务状态提交
        # 失败，也不能恢复旧文件；卡死任务恢复后会幂等完成。
        try:
            self._job_service.mark_succeeded(
                scope=scope,
                job_id=job.id,
                indexed_chunk_count=(
                    indexed_chunk_count
                ),
            )

        except Exception:
            record_indexing_job(
                outcome="status_commit_failed",
                duration_seconds=(
                    perf_counter() - started
                ),
            )
            logger.exception(
                "索引已经完成，但任务成功状态提交失败：job_id=%s",
                job.id,
            )
            return True

        try:
            self._file_service.complete_candidate(swap)

        except OSError:
            # 正式文件和数据库已经成功，备份残留不能把任务
            # 重新标记为失败，后续孤儿文件清理可再次处理。
            logger.exception(
                "索引成功，但候选文件清理失败：job_id=%s",
                job.id,
            )

        duration_seconds = perf_counter() - started
        record_indexing_job(
            outcome="completed",
            duration_seconds=duration_seconds,
        )
        logger.info(
            event_message(
                "indexing.job.completed",
                job_id=str(job.id),
                document_id=str(job.document_id),
                tenant_id=str(job.tenant_id),
                knowledge_base_id=str(
                    job.knowledge_base_id
                ),
                attempt_count=job.attempt_count,
                indexed_chunk_count=indexed_chunk_count,
                duration_ms=round(
                    duration_seconds * 1000,
                    2,
                ),
            )
        )
        return True

    def recover_stale_jobs(self) -> int:
        """恢复本 Worker 可见的超时运行任务。"""
        return self._job_service.recover_stale_running_jobs(
            stale_after_seconds=(
                self._settings
                .indexing_job_stale_after_seconds
            )
        )

    def cleanup_expired_failed_candidates(
        self,
        *,
        limit: int = 100,
    ) -> int:
        """清理超过保留期的最终失败任务候选文件。"""
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须介于 1 和 1000 之间")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(
            hours=(
                self._settings
                .indexing_failed_file_retention_hours
            )
        )

        try:
            jobs = self._session.scalars(
                select(DocumentIndexingJob)
                .where(
                    DocumentIndexingJob.status == "failed",
                    DocumentIndexingJob.finished_at
                    <= cutoff,
                    DocumentIndexingJob
                    .staged_candidate_deleted_at
                    .is_(None),
                )
                .order_by(
                    DocumentIndexingJob.finished_at,
                    DocumentIndexingJob.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()

            cleaned = 0

            for job in jobs:
                scope = RetrievalScope(
                    tenant_id=job.tenant_id,
                    knowledge_base_id=(
                        job.knowledge_base_id
                    ),
                )

                try:
                    self._file_service.discard_staged_candidate(
                        staged_storage_uri=(
                            job.staged_storage_uri
                        ),
                        scope=scope,
                    )
                except (OSError, ValueError):
                    logger.exception(
                        "过期索引候选文件清理失败：job_id=%s",
                        job.id,
                    )
                    continue

                job.staged_candidate_deleted_at = now
                job.reservation_released_at = now
                cleaned += 1

            self._session.commit()
            return cleaned

        except Exception:
            self._session.rollback()
            raise

    def _get_document(
        self,
        *,
        job: DocumentIndexingJob,
        scope: RetrievalScope,
    ) -> KnowledgeDocument:
        document = self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.id
                == job.document_id,
            )
        )

        if document is None:
            self._session.rollback()
            raise LookupError(
                "索引任务对应的文档不存在"
            )

        return document

    def _mark_document_indexing(
        self,
        document: KnowledgeDocument,
    ) -> None:
        if document.status == "ready":
            self._session.rollback()
            return

        document.status = "indexing"
        document.last_error = None
        self._session.commit()

    def _handle_failure(
        self,
        *,
        job: DocumentIndexingJob,
        scope: RetrievalScope,
        swap: CandidateFileSwap | None,
        error: Exception,
        duration_ms: float,
    ) -> None:
        error_message = (
            str(error).strip()
            or error.__class__.__name__
        )
        if swap is not None:
            try:
                self._file_service.restore_candidate(swap)

            except OSError as restore_error:
                error_message = (
                    f"{error_message}; 文件恢复失败："
                    f"{restore_error}"
                )
                logger.exception(
                    "索引失败且文件恢复失败：job_id=%s",
                    job.id,
                )

        try:
            failed_job = self._job_service.mark_failed(
                scope=scope,
                job_id=job.id,
                error=error_message,
            )

        except Exception:
            logger.exception(
                "索引失败状态提交失败：job_id=%s",
                job.id,
            )
            return

        self._update_failed_document_status(
            job=failed_job,
            error=error_message,
        )
        record_indexing_job(
            outcome=(
                "retry_scheduled"
                if failed_job.status == "queued"
                else "failed"
            ),
            duration_seconds=duration_ms / 1000,
        )

        # 最终失败也保留已经恢复到暂存目录的候选文件，
        # 允许管理员排查原因后人工重试。过期文件由后续
        # 保留期清理阶段处理，不能在这里提前删除。

        logger.warning(
            event_message(
                "indexing.job.failed",
                job_id=str(job.id),
                document_id=str(job.document_id),
                tenant_id=str(job.tenant_id),
                knowledge_base_id=str(
                    job.knowledge_base_id
                ),
                attempt_count=failed_job.attempt_count,
                status=failed_job.status,
                error_type=type(error).__name__,
                duration_ms=duration_ms,
            )
        )

    def _update_failed_document_status(
        self,
        *,
        job: DocumentIndexingJob,
        error: str,
    ) -> None:
        document = self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == job.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == job.knowledge_base_id,
                KnowledgeDocument.id == job.document_id,
            )
        )

        if document is None or document.status == "ready":
            self._session.rollback()
            return

        document.status = (
            "failed"
            if job.status == "failed"
            else "pending"
        )
        document.last_error = error[:4000]
        self._session.commit()
