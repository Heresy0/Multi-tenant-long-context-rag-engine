from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
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
from backend.app.document_indexing_job_service import (
    DocumentIndexingJobService,
)
from backend.app.document_indexing_worker import (
    DocumentIndexingWorker,
)
from backend.app.document_upload_service import (
    DocumentUploadResult,
    DocumentUploadService,
)
from backend.app.rag import calculate_source_id
from backend.app.security.retrieval_scope import RetrievalScope


@dataclass(frozen=True, slots=True)
class Scenario:
    scope: RetrievalScope
    user_id: UUID
    storage_dir: Path


class FakeCandidateIndexingService:
    def __init__(
        self,
        session: Session,
        *,
        errors: list[Exception] | None = None,
    ) -> None:
        self._session = session
        self._errors = list(errors or [])
        self.calls: list[Path] = []

    def index_document_candidate(
        self,
        *,
        file_path: str | Path,
        scope: RetrievalScope,
        document_id: UUID,
        created_by_user_id: UUID,
        target_version: int,
        candidate_file_name: str,
        candidate_mime_type: str,
        candidate_content_hash: str,
        final_storage_uri: str,
    ) -> int:
        path = Path(file_path).resolve()
        self.calls.append(path)

        if self._errors:
            raise self._errors.pop(0)

        document = self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.id == document_id,
            )
        )
        assert document is not None
        document.created_by_user_id = created_by_user_id
        document.file_name = candidate_file_name
        document.mime_type = candidate_mime_type
        document.content_hash = candidate_content_hash
        document.storage_uri = final_storage_uri
        document.version = target_version
        document.status = "ready"
        document.last_error = None
        document.metadata_json = {
            "chunk_count": 2,
            "chunking_version": "test-v1",
            "embedding_model": "test-embedding",
        }
        self._session.commit()
        return 2


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
def scenario(
    session: Session,
    tmp_path: Path,
) -> Scenario:
    tenant_id = uuid4()
    user_id = uuid4()
    knowledge_base_id = uuid4()
    session.add(
        Tenant(
            id=tenant_id,
            name="Worker 测试企业",
        )
    )
    session.flush()
    session.add_all([
        User(
            id=user_id,
            tenant_id=tenant_id,
            external_subject="worker-user",
            name="Worker User",
        ),
        KnowledgeBase(
            id=knowledge_base_id,
            tenant_id=tenant_id,
            name="技术部知识库",
            visibility="restricted",
        ),
    ])
    session.commit()

    return Scenario(
        scope=RetrievalScope(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        ),
        user_id=user_id,
        storage_dir=tmp_path / "documents",
    )


def _settings(scenario: Scenario) -> SimpleNamespace:
    return SimpleNamespace(
        document_storage_dir=scenario.storage_dir,
        indexing_job_retry_delay_seconds=0,
        indexing_job_stale_after_seconds=60,
    )


def _upload(
    session: Session,
    scenario: Scenario,
    *,
    file_name: str,
    content: bytes,
) -> DocumentUploadResult:
    return DocumentUploadService(
        session=session,
        storage_dir=scenario.storage_dir,
        max_upload_bytes=1024,
    ).upload(
        file_name=file_name,
        source=BytesIO(content),
        scope=scenario.scope,
        created_by_user_id=scenario.user_id,
    )


def _target_path(
    scenario: Scenario,
    file_name: str,
) -> Path:
    return (
        scenario.storage_dir
        / str(scenario.scope.tenant_id)
        / str(scenario.scope.knowledge_base_id)
        / file_name
    ).resolve()


def _staged_files(scenario: Scenario) -> list[Path]:
    return list(
        scenario.storage_dir.glob(
            "*/*/.staging/*"
        )
    )


def _add_ready_document(
    session: Session,
    scenario: Scenario,
    *,
    file_name: str,
    content: bytes,
) -> KnowledgeDocument:
    target_path = _target_path(scenario, file_name)
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(content)
    document = KnowledgeDocument(
        tenant_id=scenario.scope.tenant_id,
        knowledge_base_id=(
            scenario.scope.knowledge_base_id
        ),
        created_by_user_id=scenario.user_id,
        source_id=calculate_source_id(target_path),
        file_name=file_name,
        storage_uri=target_path.as_uri(),
        mime_type="text/plain",
        content_hash=sha256(content).hexdigest(),
        status="ready",
        version=1,
        metadata_json={"chunk_count": 1},
    )
    session.add(document)
    session.commit()
    return document


def test_worker_completes_new_document(
    session: Session,
    scenario: Scenario,
) -> None:
    upload = _upload(
        session,
        scenario,
        file_name="研发规范.txt",
        content=b"new-document",
    )
    fake_indexing = FakeCandidateIndexingService(session)
    worker = DocumentIndexingWorker(
        session=session,
        settings=_settings(scenario),
        indexing_service=fake_indexing,
    )

    processed = worker.run_once()

    session.expire_all()
    document = session.get(
        KnowledgeDocument,
        upload.document.id,
    )
    job = session.get(
        DocumentIndexingJob,
        upload.indexing_job.id,
    )
    target_path = _target_path(
        scenario,
        "研发规范.txt",
    )

    assert processed is True
    assert document is not None
    assert document.status == "ready"
    assert document.version == 1
    assert document.metadata_json["chunk_count"] == 2
    assert job is not None
    assert job.status == "succeeded"
    assert job.attempt_count == 1
    assert job.indexed_chunk_count == 2
    assert target_path.read_bytes() == b"new-document"
    assert fake_indexing.calls == [target_path]
    assert _staged_files(scenario) == []
    assert worker.run_once() is False


def test_worker_restores_ready_version_and_stops_after_limit(
    session: Session,
    scenario: Scenario,
) -> None:
    document = _add_ready_document(
        session,
        scenario,
        file_name="生产手册.txt",
        content=b"old-version",
    )
    upload = _upload(
        session,
        scenario,
        file_name="生产手册.txt",
        content=b"new-version",
    )
    upload.indexing_job.max_attempts = 2
    session.commit()
    fake_indexing = FakeCandidateIndexingService(
        session,
        errors=[
            RuntimeError("embedding unavailable"),
            RuntimeError("embedding unavailable"),
        ],
    )
    worker = DocumentIndexingWorker(
        session=session,
        settings=_settings(scenario),
        indexing_service=fake_indexing,
    )

    assert worker.run_once() is True
    session.expire_all()
    first_attempt = session.get(
        DocumentIndexingJob,
        upload.indexing_job.id,
    )
    assert first_attempt is not None
    assert first_attempt.status == "queued"
    assert _target_path(
        scenario,
        "生产手册.txt",
    ).read_bytes() == b"old-version"
    assert _staged_files(scenario)[0].read_bytes() == (
        b"new-version"
    )

    assert worker.run_once() is True
    session.expire_all()
    failed_job = session.get(
        DocumentIndexingJob,
        upload.indexing_job.id,
    )
    stored_document = session.get(
        KnowledgeDocument,
        document.id,
    )

    assert failed_job is not None
    assert failed_job.status == "failed"
    assert failed_job.attempt_count == 2
    assert stored_document is not None
    assert stored_document.status == "ready"
    assert stored_document.version == 1
    assert _target_path(
        scenario,
        "生产手册.txt",
    ).read_bytes() == b"old-version"
    assert _staged_files(scenario) == []


def test_worker_recovers_after_index_commit_before_job_commit(
    session: Session,
    scenario: Scenario,
) -> None:
    document = _add_ready_document(
        session,
        scenario,
        file_name="恢复测试.txt",
        content=b"old-version",
    )
    upload = _upload(
        session,
        scenario,
        file_name="恢复测试.txt",
        content=b"new-version",
    )
    claimed = DocumentIndexingJobService(
        session=session
    ).claim_next()
    assert claimed is not None
    target_path = _target_path(
        scenario,
        "恢复测试.txt",
    )
    backup_path = (
        target_path.parent
        / f".backup-{claimed.id}.tmp"
    )
    staged_path = _staged_files(scenario)[0]
    target_path.replace(backup_path)
    staged_path.replace(target_path)

    document.status = "ready"
    document.version = claimed.target_version
    document.content_hash = (
        claimed.candidate_content_hash
    )
    document.metadata_json = {"chunk_count": 2}
    claimed.started_at = (
        datetime.now(timezone.utc)
        - timedelta(minutes=10)
    )
    session.commit()

    fake_indexing = FakeCandidateIndexingService(session)
    worker = DocumentIndexingWorker(
        session=session,
        settings=_settings(scenario),
        indexing_service=fake_indexing,
    )

    assert worker.recover_stale_jobs() == 1
    assert worker.run_once() is True

    session.expire_all()
    job = session.get(DocumentIndexingJob, claimed.id)
    stored_document = session.get(
        KnowledgeDocument,
        document.id,
    )

    assert job is not None
    assert job.status == "succeeded"
    assert job.attempt_count == 2
    assert stored_document is not None
    assert stored_document.version == 2
    assert target_path.read_bytes() == b"new-version"
    assert backup_path.exists() is False
    assert _staged_files(scenario) == []
