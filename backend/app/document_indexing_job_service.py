from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import (
    DocumentIndexingJob,
    KnowledgeDocument,
)
from .security.retrieval_scope import RetrievalScope


ACTIVE_STATUSES = (
    "queued",
    "running",
)


class DocumentIndexingJobNotFound(LookupError):
    """指定范围内不存在索引任务。"""


class ActiveDocumentIndexingJobExists(RuntimeError):
    """文档已经存在待执行或正在执行的任务。"""


class InvalidJobTransition(RuntimeError):
    """索引任务状态转换不合法。"""


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
        max_attempts: int = 3,
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
                target_version=target_version,
                status="queued",
                attempt_count=0,
                max_attempts=max_attempts,
                available_at=self._now(),
            )
            self._session.add(job)
            self._session.commit()
            self._session.refresh(job)
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
            job.status = "succeeded"
            job.indexed_chunk_count = (
                indexed_chunk_count
            )
            job.finished_at = self._now()
            job.last_error = None

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