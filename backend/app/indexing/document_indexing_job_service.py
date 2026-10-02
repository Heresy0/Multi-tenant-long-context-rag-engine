from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


ACTIVE_STATUSES = (
    "queued",
    "running",
)

JOB_STATUSES = (
    *ACTIVE_STATUSES,
    "succeeded",
    "failed",
)


class DocumentIndexingJobNotFound(LookupError):
    """指定范围内不存在索引任务。"""


class ActiveDocumentIndexingJobExists(RuntimeError):
    """文档已经存在待执行或正在执行的任务。"""


class InvalidJobTransition(RuntimeError):
    """索引任务状态转换不合法。"""


class DocumentIndexingJobConflict(RuntimeError):
    """索引任务与文档当前状态冲突。"""


class DocumentIndexingJobService:
    """创建、领取及更新文档索引任务。"""

    def __init__(
        self,
        *,
        session: Session,
        retry_delay_seconds: float = 30,
    ) -> None:
        if retry_delay_seconds < 0:
            raise ValueError(
                "retry_delay_seconds 不能小于 0"
            )

        self._session = session
        self._retry_delay = timedelta(
            seconds=retry_delay_seconds
        )

    def create(
        self,
        *,
        scope: RetrievalScope,
        document_id: UUID,
        requested_by_user_id: UUID,
        staged_storage_uri: str,
        candidate_file_name: str,
        candidate_mime_type: str,
        candidate_content_hash: str,
        target_version: int,
        candidate_size_bytes: int = 0,
        max_attempts: int = 3,
        commit: bool = True,
    ) -> DocumentIndexingJob:
        """为指定范围内的文档创建待执行任务。"""
        if target_version < 1:
            raise ValueError(
                "target_version 必须大于等于 1"
            )

        if max_attempts < 1:
            raise ValueError(
                "max_attempts 必须大于等于 1"
            )

        if candidate_size_bytes < 0:
            raise ValueError(
                "candidate_size_bytes 不能小于 0"
            )

        values = {
            "staged_storage_uri": staged_storage_uri,
            "candidate_file_name": candidate_file_name,
            "candidate_mime_type": candidate_mime_type,
            "candidate_content_hash": (
                candidate_content_hash
            ),
        }

        if any(
            not value.strip()
            for value in values.values()
        ):
            raise ValueError(
                "索引任务的候选文件信息不能为空"
            )

        try:
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
                raise DocumentIndexingJobNotFound(
                    "文档不存在。"
                )

            active_job = self._session.scalar(
                select(DocumentIndexingJob.id)
                .where(
                    DocumentIndexingJob.tenant_id
                    == scope.tenant_id,
                    DocumentIndexingJob.knowledge_base_id
                    == scope.knowledge_base_id,
                    DocumentIndexingJob.document_id
                    == document_id,
                    DocumentIndexingJob.status.in_(
                        ACTIVE_STATUSES
                    ),
                )
            )

            if active_job is not None:
                raise ActiveDocumentIndexingJobExists(
                    "该文档已经存在活动索引任务。"
                )

            job = DocumentIndexingJob(
                tenant_id=scope.tenant_id,
                knowledge_base_id=(
                    scope.knowledge_base_id
                ),
                document_id=document_id,
                requested_by_user_id=(
                    requested_by_user_id
                ),
                staged_storage_uri=(
                    staged_storage_uri
                ),
                candidate_file_name=(
                    candidate_file_name
                ),
                candidate_mime_type=(
                    candidate_mime_type
                ),
                candidate_content_hash=(
                    candidate_content_hash
                ),
                candidate_size_bytes=(
                    candidate_size_bytes
                ),
                target_version=target_version,
                status="queued",
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=self._now(),
            )
            self._session.add(job)
            if commit:
                self._session.commit()
                self._session.refresh(job)
            else:
                self._session.flush()
            return job

        except IntegrityError as exc:
            self._session.rollback()

            if (
                "uq_indexing_jobs_active_document"
                in str(exc)
                or "UNIQUE constraint failed"
                in str(exc)
            ):
                raise ActiveDocumentIndexingJobExists(
                    "该文档已经存在活动索引任务。"
                ) from exc

            raise

        except Exception:
            self._session.rollback()
            raise

    def claim_next(
        self,
    ) -> DocumentIndexingJob | None:
        """原子领取下一条可执行任务。"""
        now = self._now()

        try:
            job = self._session.scalar(
                select(DocumentIndexingJob)
                .where(
                    DocumentIndexingJob.status
                    == "queued",
                    DocumentIndexingJob.available_at
                    <= now,
                    DocumentIndexingJob.attempt_count
                    < DocumentIndexingJob.max_attempts,
                )
                .order_by(
                    DocumentIndexingJob.available_at,
                    DocumentIndexingJob.created_at,
                    DocumentIndexingJob.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )

            if job is None:
                self._session.rollback()
                return None

            job.status = "running"
            job.attempt_count += 1
            job.started_at = now
            job.finished_at = None

            self._session.commit()
            self._session.refresh(job)
            return job

        except Exception:
            self._session.rollback()
            raise

    def mark_succeeded(
        self,
        *,
        scope: RetrievalScope,
        job_id: UUID,
        indexed_chunk_count: int,
    ) -> DocumentIndexingJob:
        """将运行中的任务标记为成功。"""
        if indexed_chunk_count < 0:
            raise ValueError(
                "indexed_chunk_count 不能小于 0"
            )

        job = self._get_locked(
            scope=scope,
            job_id=job_id,
        )

        if job.status != "running":
            self._session.rollback()
            raise InvalidJobTransition(
                "只有运行中的任务可以标记为成功。"
            )

        try:
            document = self._session.scalar(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id
                    == scope.tenant_id,
                    KnowledgeDocument.knowledge_base_id
                    == scope.knowledge_base_id,
                    KnowledgeDocument.id
                    == job.document_id,
                )
                .with_for_update()
            )
            if document is None:
                raise DocumentIndexingJobNotFound(
                    "索引任务对应的文档不存在。"
                )

            now = self._now()
            job.status = "succeeded"
            job.indexed_chunk_count = (
                indexed_chunk_count
            )
            job.finished_at = now
            job.last_error = None
            job.reservation_released_at = now
            document.file_size_bytes = (
                job.candidate_size_bytes
            )

            self._session.commit()
            self._session.refresh(job)
            return job

        except Exception:
            self._session.rollback()
            raise

    def mark_failed(
        self,
        *,
        scope: RetrievalScope,
        job_id: UUID,
        error: str,
    ) -> DocumentIndexingJob:
        """记录失败；有剩余次数时重新排队。"""
        normalized_error = error.strip()

        if not normalized_error:
            raise ValueError("error 不能为空")

        job = self._get_locked(
            scope=scope,
            job_id=job_id,
        )

        if job.status != "running":
            self._session.rollback()
            raise InvalidJobTransition(
                "只有运行中的任务可以记录失败。"
            )

        try:
            now = self._now()
            job.last_error = normalized_error[:4000]
            job.indexed_chunk_count = None

            if job.attempt_count < job.max_attempts:
                job.status = "queued"
                job.available_at = (
                    now + self._retry_delay
                )
                job.started_at = None
                job.finished_at = None
            else:
                job.status = "failed"
                job.finished_at = now

            self._session.commit()
            self._session.refresh(job)
            return job

        except Exception:
            self._session.rollback()
            raise

    def get(
        self,
        *,
        scope: RetrievalScope,
        job_id: UUID,
    ) -> DocumentIndexingJob:
        """在租户和知识库范围内读取任务。"""
        job = self._session.scalar(
            select(DocumentIndexingJob).where(
                DocumentIndexingJob.tenant_id
                == scope.tenant_id,
                DocumentIndexingJob.knowledge_base_id
                == scope.knowledge_base_id,
                DocumentIndexingJob.id
                == job_id,
            )
        )

        if job is None:
            raise DocumentIndexingJobNotFound(
                "索引任务不存在。"
            )

        return job

    def list_jobs(
        self,
        *,
        scope: RetrievalScope,
        status: str | None = None,
        document_id: UUID | None = None,
        limit: int = 50,
    ) -> list[DocumentIndexingJob]:
        """按知识库范围列出最近的索引任务。"""
        if status is not None and status not in JOB_STATUSES:
            raise ValueError("未知的索引任务状态")

        if not 1 <= limit <= 100:
            raise ValueError("limit 必须介于 1 和 100 之间")

        statement = select(DocumentIndexingJob).where(
            DocumentIndexingJob.tenant_id
            == scope.tenant_id,
            DocumentIndexingJob.knowledge_base_id
            == scope.knowledge_base_id,
        )

        if status is not None:
            statement = statement.where(
                DocumentIndexingJob.status == status
            )

        if document_id is not None:
            statement = statement.where(
                DocumentIndexingJob.document_id
                == document_id
            )

        return list(
            self._session.scalars(
                statement.order_by(
                    DocumentIndexingJob.created_at.desc(),
                    DocumentIndexingJob.id.desc(),
                ).limit(limit)
            ).all()
        )

    def retry_failed(
        self,
        *,
        scope: RetrievalScope,
        job_id: UUID,
        candidate_validator: Callable[
            [DocumentIndexingJob],
            None,
        ] | None = None,
        actor_user_id: UUID | None = None,
        request_id: str | None = None,
    ) -> DocumentIndexingJob:
        """把仍然有效的最终失败任务重新加入队列。"""
        job = self._get_locked(
            scope=scope,
            job_id=job_id,
        )

        if job.status != "failed":
            self._session.rollback()
            raise InvalidJobTransition(
                "只有最终失败的任务可以人工重试。"
            )

        try:
            active_job_id = self._session.scalar(
                select(DocumentIndexingJob.id).where(
                    DocumentIndexingJob.tenant_id
                    == scope.tenant_id,
                    DocumentIndexingJob.knowledge_base_id
                    == scope.knowledge_base_id,
                    DocumentIndexingJob.document_id
                    == job.document_id,
                    DocumentIndexingJob.id != job.id,
                    DocumentIndexingJob.status.in_(
                        ACTIVE_STATUSES
                    ),
                )
            )

            if active_job_id is not None:
                raise DocumentIndexingJobConflict(
                    "该文档已经存在活动索引任务。"
                )

            document = self._session.scalar(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id
                    == scope.tenant_id,
                    KnowledgeDocument.knowledge_base_id
                    == scope.knowledge_base_id,
                    KnowledgeDocument.id
                    == job.document_id,
                )
                .with_for_update()
            )

            if document is None:
                raise DocumentIndexingJobNotFound(
                    "索引任务对应的文档不存在。"
                )

            if (
                document.status == "ready"
                and document.version >= job.target_version
            ):
                raise DocumentIndexingJobConflict(
                    "该失败任务对应的文档版本已经过期。"
                )

            if job.staged_candidate_deleted_at is not None:
                raise DocumentIndexingJobConflict(
                    "索引候选文件已经过期，请重新上传文档。"
                )

            if candidate_validator is not None:
                candidate_validator(job)

            job.status = "queued"
            job.attempt_count = 0
            job.indexed_chunk_count = None
            job.available_at = self._now()
            job.started_at = None
            job.finished_at = None
            job.last_error = None
            job.staged_candidate_deleted_at = None

            if document.status != "ready":
                document.status = "pending"
                document.last_error = None

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
                    action="indexing_job.retry_requested",
                    resource_type="indexing_job",
                    resource_id=job.id,
                    outcome="success",
                    request_id=request_id,
                    details={
                        "document_id": str(job.document_id),
                        "target_version": job.target_version,
                    },
                )

            self._session.commit()
            self._session.refresh(job)
            if audit_event is not None:
                log_committed_audit_event(audit_event)
            return job

        except IntegrityError as exc:
            self._session.rollback()
            raise DocumentIndexingJobConflict(
                "该文档已经存在活动索引任务。"
            ) from exc

        except Exception:
            self._session.rollback()
            raise

    def recover_stale_running_jobs(
        self,
        *,
        stale_after_seconds: float,
    ) -> int:
        """重新排队或终止超过运行时限的任务。"""
        if stale_after_seconds < 0:
            raise ValueError(
                "stale_after_seconds 不能小于 0"
            )

        now = self._now()
        stale_before = now - timedelta(
            seconds=stale_after_seconds
        )

        try:
            jobs = self._session.scalars(
                select(DocumentIndexingJob)
                .where(
                    DocumentIndexingJob.status
                    == "running",
                    DocumentIndexingJob.started_at
                    <= stale_before,
                )
                .order_by(
                    DocumentIndexingJob.started_at,
                    DocumentIndexingJob.id,
                )
                .with_for_update(skip_locked=True)
            ).all()

            for job in jobs:
                job.last_error = (
                    "索引 Worker 超过运行时限，任务已恢复。"
                )
                job.indexed_chunk_count = None
                job.status = "queued"
                job.available_at = now
                job.started_at = None
                job.finished_at = None

                # 进程中断没有机会执行文件恢复，因此最后一次
                # claim 不能直接耗尽重试次数，要保留一次恢复执行。
                if job.attempt_count >= job.max_attempts:
                    job.attempt_count = max(
                        job.max_attempts - 1,
                        0,
                    )

            self._session.commit()
            return len(jobs)

        except Exception:
            self._session.rollback()
            raise

    def _get_locked(
        self,
        *,
        scope: RetrievalScope,
        job_id: UUID,
    ) -> DocumentIndexingJob:
        job = self._session.scalar(
            select(DocumentIndexingJob)
            .where(
                DocumentIndexingJob.tenant_id
                == scope.tenant_id,
                DocumentIndexingJob.knowledge_base_id
                == scope.knowledge_base_id,
                DocumentIndexingJob.id
                == job_id,
            )
            .with_for_update()
        )

        if job is None:
            self._session.rollback()
            raise DocumentIndexingJobNotFound(
                "索引任务不存在。"
            )

        return job

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
