from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    DocumentIndexingJob,
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
    User,
)
from backend.app.indexing.document_indexing_job_service import (
    ActiveDocumentIndexingJobExists,
    DocumentIndexingJobConflict,
    DocumentIndexingJobNotFound,
    DocumentIndexingJobService,
    InvalidJobTransition,
)
from backend.app.security.retrieval_scope import RetrievalScope


@dataclass(frozen=True, slots=True)
class Scenario:
    tenant_id: UUID
    user_id: UUID
    primary_scope: RetrievalScope
    other_scope: RetrievalScope
    primary_document_id: UUID
    other_document_id: UUID


@pytest.fixture
def session() -> Iterator[Session]:
    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={
            "check_same_thread": False,
        },
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(
        dbapi_connection,
        _connection_record,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    Base.metadata.create_all(engine)

    with Session(engine) as database_session:
        yield database_session

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def scenario(session: Session) -> Scenario:
    tenant_id = uuid4()
    user_id = uuid4()
    primary_kb_id = uuid4()
    other_kb_id = uuid4()

    session.add(
        Tenant(
            id=tenant_id,
            name="异步索引测试企业",
        )
    )
    session.flush()

    session.add_all([
        User(
            id=user_id,
            tenant_id=tenant_id,
            external_subject="keycloak-indexing-user",
            name="Indexing User",
        ),
        KnowledgeBase(
            id=primary_kb_id,
            tenant_id=tenant_id,
            name="技术部知识库",
            visibility="restricted",
        ),
        KnowledgeBase(
            id=other_kb_id,
            tenant_id=tenant_id,
            name="人力资源部知识库",
            visibility="restricted",
        ),
    ])
    session.flush()

    primary_document = _add_document(
        session,
        tenant_id=tenant_id,
        knowledge_base_id=primary_kb_id,
        user_id=user_id,
        marker="a",
    )
    other_document = _add_document(
        session,
        tenant_id=tenant_id,
        knowledge_base_id=other_kb_id,
        user_id=user_id,
        marker="b",
    )
    session.commit()

    return Scenario(
        tenant_id=tenant_id,
        user_id=user_id,
        primary_scope=RetrievalScope(
            tenant_id=tenant_id,
            knowledge_base_id=primary_kb_id,
        ),
        other_scope=RetrievalScope(
            tenant_id=tenant_id,
            knowledge_base_id=other_kb_id,
        ),
        primary_document_id=primary_document.id,
        other_document_id=other_document.id,
    )


def _add_document(
    session: Session,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    user_id: UUID,
    marker: str,
) -> KnowledgeDocument:
    document = KnowledgeDocument(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        created_by_user_id=user_id,
        source_id=marker * 64,
        file_name=f"{marker}.txt",
        storage_uri=f"file:///documents/{marker}.txt",
        mime_type="text/plain",
        content_hash=marker * 64,
        status="ready",
        version=1,
        metadata_json={},
    )
    session.add(document)
    session.flush()
    return document


def _create_job(
    service: DocumentIndexingJobService,
    scenario: Scenario,
    *,
    scope: RetrievalScope | None = None,
    document_id: UUID | None = None,
    marker: str = "c",
    target_version: int = 2,
    max_attempts: int = 3,
) -> DocumentIndexingJob:
    return service.create(
        scope=scope or scenario.primary_scope,
        document_id=(
            document_id
            or scenario.primary_document_id
        ),
        requested_by_user_id=scenario.user_id,
        staged_storage_uri=(
            f"file:///staging/{marker}.txt"
        ),
        candidate_file_name=f"{marker}.txt",
        candidate_mime_type="text/plain",
        candidate_content_hash=marker * 64,
        target_version=target_version,
        max_attempts=max_attempts,
    )


def test_creates_queued_job_with_candidate_metadata(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )

    job = _create_job(service, scenario)

    stored = session.get(DocumentIndexingJob, job.id)

    assert stored is not None
    assert stored.tenant_id == scenario.tenant_id
    assert (
        stored.knowledge_base_id
        == scenario.primary_scope.knowledge_base_id
    )
    assert stored.document_id == scenario.primary_document_id
    assert stored.requested_by_user_id == scenario.user_id
    assert stored.status == "queued"
    assert stored.attempt_count == 0
    assert stored.max_attempts == 3
    assert stored.target_version == 2
    assert stored.candidate_file_name == "c.txt"
    assert stored.candidate_content_hash == "c" * 64


def test_blocks_second_active_job_for_same_document(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )
    first = _create_job(service, scenario)

    with pytest.raises(
        ActiveDocumentIndexingJobExists,
        match="活动索引任务",
    ):
        _create_job(
            service,
            scenario,
            marker="d",
        )

    stored = session.scalars(
        select(DocumentIndexingJob).where(
            DocumentIndexingJob.document_id
            == scenario.primary_document_id
        )
    ).all()

    assert [job.id for job in stored] == [first.id]


def test_claims_available_job_and_marks_it_succeeded(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )
    queued = _create_job(service, scenario)

    claimed = service.claim_next()

    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == "running"
    assert claimed.attempt_count == 1
    assert claimed.started_at is not None

    completed = service.mark_succeeded(
        scope=scenario.primary_scope,
        job_id=claimed.id,
        indexed_chunk_count=12,
    )

    assert completed.status == "succeeded"
    assert completed.indexed_chunk_count == 12
    assert completed.finished_at is not None
    assert completed.last_error is None


def test_skips_job_that_is_not_available_yet(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )
    job = _create_job(service, scenario)
    job.available_at = (
        datetime.now(timezone.utc)
        + timedelta(hours=1)
    )
    session.commit()

    claimed = service.claim_next()

    assert claimed is None
    session.expire_all()
    stored = session.get(DocumentIndexingJob, job.id)
    assert stored is not None
    assert stored.status == "queued"
    assert stored.attempt_count == 0


def test_requeues_failure_then_stops_at_attempt_limit(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session,
        retry_delay_seconds=0,
    )
    job = _create_job(
        service,
        scenario,
        max_attempts=2,
    )

    first_attempt = service.claim_next()
    assert first_attempt is not None

    retried = service.mark_failed(
        scope=scenario.primary_scope,
        job_id=job.id,
        error="temporary model failure",
    )

    assert retried.status == "queued"
    assert retried.attempt_count == 1
    assert retried.started_at is None
    assert retried.finished_at is None
    assert retried.last_error == "temporary model failure"

    second_attempt = service.claim_next()
    assert second_attempt is not None
    assert second_attempt.attempt_count == 2

    failed = service.mark_failed(
        scope=scenario.primary_scope,
        job_id=job.id,
        error="permanent model failure",
    )

    assert failed.status == "failed"
    assert failed.attempt_count == 2
    assert failed.finished_at is not None
    assert failed.last_error == "permanent model failure"
    assert service.claim_next() is None


def test_rejects_invalid_transition_and_allows_new_job_after_success(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )
    first = _create_job(service, scenario)

    with pytest.raises(
        InvalidJobTransition,
        match="运行中的任务",
    ):
        service.mark_succeeded(
            scope=scenario.primary_scope,
            job_id=first.id,
            indexed_chunk_count=1,
        )

    claimed = service.claim_next()
    assert claimed is not None
    service.mark_succeeded(
        scope=scenario.primary_scope,
        job_id=claimed.id,
        indexed_chunk_count=1,
    )

    second = _create_job(
        service,
        scenario,
        marker="d",
        target_version=3,
    )

    assert second.id != first.id
    assert second.status == "queued"


def test_does_not_read_or_create_job_across_knowledge_bases(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )
    job = _create_job(service, scenario)

    with pytest.raises(
        DocumentIndexingJobNotFound,
        match="索引任务不存在",
    ):
        service.get(
            scope=scenario.other_scope,
            job_id=job.id,
        )

    with pytest.raises(
        DocumentIndexingJobNotFound,
        match="文档不存在",
    ):
        _create_job(
            service,
            scenario,
            scope=scenario.primary_scope,
            document_id=scenario.other_document_id,
            marker="e",
        )

    stored = service.get(
        scope=scenario.primary_scope,
        job_id=job.id,
    )
    assert stored.status == "queued"


def test_recovers_stale_running_jobs_and_preserves_cleanup_attempt(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )
    retryable = _create_job(
        service,
        scenario,
        max_attempts=3,
    )
    terminal = _create_job(
        service,
        scenario,
        scope=scenario.other_scope,
        document_id=scenario.other_document_id,
        marker="f",
        max_attempts=1,
    )
    stale_time = (
        datetime.now(timezone.utc)
        - timedelta(minutes=10)
    )

    retryable.status = "running"
    retryable.attempt_count = 1
    retryable.started_at = stale_time
    terminal.status = "running"
    terminal.attempt_count = 1
    terminal.started_at = stale_time
    session.commit()

    recovered_count = (
        service.recover_stale_running_jobs(
            stale_after_seconds=60,
        )
    )

    session.expire_all()
    stored_retryable = session.get(
        DocumentIndexingJob,
        retryable.id,
    )
    stored_terminal = session.get(
        DocumentIndexingJob,
        terminal.id,
    )

    assert recovered_count == 2
    assert stored_retryable is not None
    assert stored_retryable.status == "queued"
    assert stored_retryable.started_at is None
    assert stored_terminal is not None
    assert stored_terminal.status == "queued"
    assert stored_terminal.attempt_count == 0
    assert stored_terminal.finished_at is None
    assert "超过运行时限" in stored_terminal.last_error


def test_lists_jobs_only_inside_scope_with_filters(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session
    )
    primary = _create_job(service, scenario)
    claimed = service.claim_next()
    assert claimed is not None
    service.mark_succeeded(
        scope=scenario.primary_scope,
        job_id=primary.id,
        indexed_chunk_count=3,
    )
    other = _create_job(
        service,
        scenario,
        scope=scenario.other_scope,
        document_id=scenario.other_document_id,
        marker="d",
    )

    primary_jobs = service.list_jobs(
        scope=scenario.primary_scope,
        status="succeeded",
        document_id=scenario.primary_document_id,
        limit=10,
    )

    assert [job.id for job in primary_jobs] == [
        primary.id,
    ]
    assert other.id not in {
        job.id
        for job in primary_jobs
    }

    with pytest.raises(ValueError, match="未知"):
        service.list_jobs(
            scope=scenario.primary_scope,
            status="cancelled",
        )


def test_retries_failed_job_and_resets_attempt_state(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session,
        retry_delay_seconds=0,
    )
    job = _create_job(
        service,
        scenario,
        max_attempts=1,
    )
    document = session.get(
        KnowledgeDocument,
        scenario.primary_document_id,
    )
    assert document is not None
    document.status = "failed"
    document.last_error = "embedding unavailable"
    session.commit()
    claimed = service.claim_next()
    assert claimed is not None
    service.mark_failed(
        scope=scenario.primary_scope,
        job_id=job.id,
        error="embedding unavailable",
    )

    retried = service.retry_failed(
        scope=scenario.primary_scope,
        job_id=job.id,
    )

    assert retried.status == "queued"
    assert retried.attempt_count == 0
    assert retried.last_error is None
    assert retried.started_at is None
    assert retried.finished_at is None
    assert document.status == "pending"
    assert document.last_error is None


def test_rejects_retry_when_job_is_active_or_stale(
    session: Session,
    scenario: Scenario,
) -> None:
    service = DocumentIndexingJobService(
        session=session,
        retry_delay_seconds=0,
    )
    failed = _create_job(
        service,
        scenario,
        max_attempts=1,
    )
    claimed = service.claim_next()
    assert claimed is not None
    service.mark_failed(
        scope=scenario.primary_scope,
        job_id=failed.id,
        error="permanent failure",
    )
    active = _create_job(
        service,
        scenario,
        marker="d",
        target_version=3,
    )

    with pytest.raises(
        DocumentIndexingJobConflict,
        match="活动索引任务",
    ):
        service.retry_failed(
            scope=scenario.primary_scope,
            job_id=failed.id,
        )

    active.status = "succeeded"
    document = session.get(
        KnowledgeDocument,
        scenario.primary_document_id,
    )
    assert document is not None
    document.status = "ready"
    document.version = failed.target_version
    session.commit()

    with pytest.raises(
        DocumentIndexingJobConflict,
        match="版本已经过期",
    ):
        service.retry_failed(
            scope=scenario.primary_scope,
            job_id=failed.id,
        )
