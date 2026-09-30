from collections.abc import Iterator
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, func, select
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
    ActiveDocumentIndexingJobExists,
)
from backend.app.document_upload_service import (
    DocumentUploadConflict,
    DocumentUploadService,
    UnsupportedUploadType,
    UploadTooLarge,
)
from backend.app.rag import calculate_source_id
from backend.app.security.retrieval_scope import RetrievalScope


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
def scope_and_user(
    session: Session,
) -> tuple[RetrievalScope, UUID]:
    tenant_id = uuid4()
    user_id = uuid4()
    knowledge_base_id = uuid4()

    session.add(
        Tenant(
            id=tenant_id,
            name="异步上传测试企业",
        )
    )
    session.flush()
    session.add_all([
        User(
            id=user_id,
            tenant_id=tenant_id,
            external_subject="keycloak-alice",
            name="Alice",
        ),
        KnowledgeBase(
            id=knowledge_base_id,
            tenant_id=tenant_id,
            name="技术部知识库",
            visibility="restricted",
        ),
    ])
    session.commit()

    return (
        RetrievalScope(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        ),
        user_id,
    )


def _service(
    session: Session,
    storage_dir: Path,
    *,
    max_upload_bytes: int = 1024,
) -> DocumentUploadService:
    return DocumentUploadService(
        session=session,
        storage_dir=storage_dir,
        max_upload_bytes=max_upload_bytes,
    )


def _target_path(
    storage_dir: Path,
    scope: RetrievalScope,
    file_name: str,
) -> Path:
    return (
        storage_dir
        / str(scope.tenant_id)
        / str(scope.knowledge_base_id)
        / file_name
    ).resolve()


def _staged_files(target_path: Path) -> list[Path]:
    staging_directory = target_path.parent / ".staging"

    if not staging_directory.exists():
        return []

    return [
        path
        for path in staging_directory.iterdir()
        if path.is_file()
    ]


def test_upload_creates_pending_document_and_queued_job(
    session: Session,
    scope_and_user: tuple[RetrievalScope, UUID],
    tmp_path: Path,
) -> None:
    scope, user_id = scope_and_user
    storage_dir = tmp_path / "documents"
    content = "等待异步索引的企业资料".encode("utf-8")

    result = _service(session, storage_dir).upload(
        file_name="研发规范.txt",
        source=BytesIO(content),
        scope=scope,
        created_by_user_id=user_id,
    )

    target_path = _target_path(
        storage_dir,
        scope,
        "研发规范.txt",
    )
    staged_files = _staged_files(target_path)

    assert target_path.exists() is False
    assert len(staged_files) == 1
    assert staged_files[0].read_bytes() == content
    assert result.document.status == "pending"
    assert result.document.version == 1
    assert result.document.metadata_json == {}
    assert result.document.source_id == (
        calculate_source_id(target_path)
    )
    assert result.document.storage_uri == target_path.as_uri()
    assert result.indexing_job.status == "queued"
    assert result.indexing_job.target_version == 1
    assert result.indexing_job.attempt_count == 0
    assert result.indexing_job.staged_storage_uri == (
        staged_files[0].resolve().as_uri()
    )
    assert result.indexing_job.candidate_content_hash == (
        sha256(content).hexdigest()
    )


def test_update_keeps_ready_document_and_old_file_available(
    session: Session,
    scope_and_user: tuple[RetrievalScope, UUID],
    tmp_path: Path,
) -> None:
    scope, user_id = scope_and_user
    storage_dir = tmp_path / "documents"
    target_path = _target_path(
        storage_dir,
        scope,
        "生产手册.txt",
    )
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(b"old-content")
    old_hash = sha256(b"old-content").hexdigest()
    document = KnowledgeDocument(
        tenant_id=scope.tenant_id,
        knowledge_base_id=scope.knowledge_base_id,
        created_by_user_id=user_id,
        source_id=calculate_source_id(target_path),
        file_name="生产手册.txt",
        storage_uri=target_path.as_uri(),
        mime_type="text/plain",
        content_hash=old_hash,
        status="ready",
        version=3,
        metadata_json={"chunk_count": 4},
    )
    session.add(document)
    session.commit()

    result = _service(session, storage_dir).upload(
        file_name="生产手册.txt",
        source=BytesIO(b"new-content"),
        scope=scope,
        created_by_user_id=user_id,
    )

    session.expire_all()
    stored = session.get(KnowledgeDocument, document.id)

    assert stored is not None
    assert target_path.read_bytes() == b"old-content"
    assert stored.status == "ready"
    assert stored.version == 3
    assert stored.content_hash == old_hash
    assert stored.metadata_json == {"chunk_count": 4}
    assert result.indexing_job.target_version == 4
    assert _staged_files(target_path)[0].read_bytes() == (
        b"new-content"
    )


def test_unchanged_ready_document_uses_same_target_version(
    session: Session,
    scope_and_user: tuple[RetrievalScope, UUID],
    tmp_path: Path,
) -> None:
    scope, user_id = scope_and_user
    storage_dir = tmp_path / "documents"
    target_path = _target_path(
        storage_dir,
        scope,
        "制度.txt",
    )
    target_path.parent.mkdir(parents=True)
    content = b"unchanged-content"
    target_path.write_bytes(content)
    document = KnowledgeDocument(
        tenant_id=scope.tenant_id,
        knowledge_base_id=scope.knowledge_base_id,
        created_by_user_id=user_id,
        source_id=calculate_source_id(target_path),
        file_name="制度.txt",
        storage_uri=target_path.as_uri(),
        mime_type="text/plain",
        content_hash=sha256(content).hexdigest(),
        status="ready",
        version=2,
        metadata_json={"chunk_count": 1},
    )
    session.add(document)
    session.commit()

    result = _service(session, storage_dir).upload(
        file_name="制度.txt",
        source=BytesIO(content),
        scope=scope,
        created_by_user_id=user_id,
    )

    assert result.indexing_job.target_version == 2
    assert result.document.version == 2


def test_second_active_upload_is_rejected_and_cleaned_up(
    session: Session,
    scope_and_user: tuple[RetrievalScope, UUID],
    tmp_path: Path,
) -> None:
    scope, user_id = scope_and_user
    storage_dir = tmp_path / "documents"
    service = _service(session, storage_dir)

    first = service.upload(
        file_name="并发上传.txt",
        source=BytesIO(b"first"),
        scope=scope,
        created_by_user_id=user_id,
    )

    with pytest.raises(
        ActiveDocumentIndexingJobExists,
        match="活动索引任务",
    ):
        service.upload(
            file_name="并发上传.txt",
            source=BytesIO(b"second"),
            scope=scope,
            created_by_user_id=user_id,
        )

    target_path = _target_path(
        storage_dir,
        scope,
        "并发上传.txt",
    )
    staged_files = _staged_files(target_path)
    job_count = session.scalar(
        select(func.count())
        .select_from(DocumentIndexingJob)
    )

    assert job_count == 1
    assert len(staged_files) == 1
    assert staged_files[0].as_uri() == (
        first.indexing_job.staged_storage_uri
    )
    assert staged_files[0].read_bytes() == b"first"


def test_upload_validation_failure_does_not_create_records(
    session: Session,
    scope_and_user: tuple[RetrievalScope, UUID],
    tmp_path: Path,
) -> None:
    scope, user_id = scope_and_user
    storage_dir = tmp_path / "documents"
    service = _service(session, storage_dir)

    with pytest.raises(UnsupportedUploadType):
        service.upload(
            file_name="program.exe",
            source=BytesIO(b"content"),
            scope=scope,
            created_by_user_id=user_id,
        )

    with pytest.raises(UploadTooLarge):
        _service(
            session,
            storage_dir,
            max_upload_bytes=4,
        ).upload(
            file_name="oversized.txt",
            source=BytesIO(b"12345"),
            scope=scope,
            created_by_user_id=user_id,
        )

    document_count = session.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
    )
    job_count = session.scalar(
        select(func.count())
        .select_from(DocumentIndexingJob)
    )

    assert document_count == 0
    assert job_count == 0
    assert [
        path
        for path in storage_dir.rglob("*")
        if path.is_file()
    ] == []


def test_database_failure_removes_staged_file(
    session: Session,
    scope_and_user: tuple[RetrievalScope, UUID],
    tmp_path: Path,
) -> None:
    scope, _user_id = scope_and_user
    storage_dir = tmp_path / "documents"

    with pytest.raises(DocumentUploadConflict):
        _service(session, storage_dir).upload(
            file_name="无效用户.txt",
            source=BytesIO(b"content"),
            scope=scope,
            created_by_user_id=uuid4(),
        )

    target_path = _target_path(
        storage_dir,
        scope,
        "无效用户.txt",
    )

    assert _staged_files(target_path) == []
    assert session.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
    ) == 0
