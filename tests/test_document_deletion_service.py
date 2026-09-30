from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    DocumentIndexingJob,
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
    User,
)
from backend.app.document_deletion_service import (
    DocumentDeletionService,
    DocumentNotFoundError,
)
from backend.app.security.retrieval_scope import RetrievalScope


@dataclass(frozen=True, slots=True)
class ScopeData:
    tenant_id: UUID
    user_id: UUID
    primary_kb_id: UUID
    other_kb_id: UUID

    @property
    def primary_scope(self) -> RetrievalScope:
        return RetrievalScope(
            tenant_id=self.tenant_id,
            knowledge_base_id=self.primary_kb_id,
        )

    @property
    def other_scope(self) -> RetrievalScope:
        return RetrievalScope(
            tenant_id=self.tenant_id,
            knowledge_base_id=self.other_kb_id,
        )


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
def scope_data(session: Session) -> ScopeData:
    tenant_id = uuid4()
    user_id = uuid4()
    primary_kb_id = uuid4()
    other_kb_id = uuid4()

    session.add(
        Tenant(
            id=tenant_id,
            name="测试企业",
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
    session.commit()

    return ScopeData(
        tenant_id=tenant_id,
        user_id=user_id,
        primary_kb_id=primary_kb_id,
        other_kb_id=other_kb_id,
    )


def _create_document(
    session: Session,
    *,
    scope: RetrievalScope,
    user_id: UUID,
    storage_uri: str,
) -> KnowledgeDocument:
    document = KnowledgeDocument(
        tenant_id=scope.tenant_id,
        knowledge_base_id=scope.knowledge_base_id,
        created_by_user_id=user_id,
        source_id=uuid4().hex,
        file_name="删除测试 文档.txt",
        storage_uri=storage_uri,
        mime_type="text/plain",
        content_hash="a" * 64,
        status="ready",
        version=1,
        metadata_json={
            "chunk_count": 1,
        },
    )
    session.add(document)
    session.flush()

    session.add(
        DocumentChunk(
            id=uuid4().hex,
            tenant_id=scope.tenant_id,
            knowledge_base_id=scope.knowledge_base_id,
            document_id=document.id,
            chunk_index=0,
            content="用于验证删除的文档内容。",
            content_hash="b" * 64,
            parent_id=None,
            chunking_version="test-v1",
            embedding_model="test-embedding",
            embedding=[0.0] * EMBEDDING_DIMENSION,
            metadata_json={},
        )
    )
    session.commit()
    return document


def _document_count(
    session: Session,
    document_id: UUID,
) -> int:
    return session.scalar(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(KnowledgeDocument.id == document_id)
    ) or 0


def _chunk_count(
    session: Session,
    document_id: UUID,
) -> int:
    return session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
    ) or 0


def _staged_files(directory: Path) -> list[Path]:
    return list(directory.glob(".deleting-*.tmp"))


def test_deletes_managed_file_document_and_chunks(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "managed documents"
    managed_file = (
        storage_dir
        / str(scope_data.tenant_id)
        / str(scope_data.primary_kb_id)
        / "删除测试 文档.txt"
    )
    managed_file.parent.mkdir(parents=True)
    managed_file.write_text(
        "需要删除的内容",
        encoding="utf-8",
    )
    document = _create_document(
        session,
        scope=scope_data.primary_scope,
        user_id=scope_data.user_id,
        storage_uri=managed_file.as_uri(),
    )
    document_id = document.id

    service = DocumentDeletionService(
        session=session,
        storage_dir=storage_dir,
    )
    service.delete(
        scope=scope_data.primary_scope,
        document_id=document_id,
    )

    assert managed_file.exists() is False
    assert _document_count(session, document_id) == 0
    assert _chunk_count(session, document_id) == 0
    assert _staged_files(managed_file.parent) == []


def test_deletes_queued_job_and_its_staged_candidate(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "documents"
    final_path = (
        storage_dir
        / str(scope_data.tenant_id)
        / str(scope_data.primary_kb_id)
        / "待索引.txt"
    )
    staging_directory = final_path.parent / ".staging"
    staging_directory.mkdir(parents=True)
    candidate_path = staging_directory / "candidate.txt"
    candidate_path.write_bytes(b"candidate")
    document = KnowledgeDocument(
        tenant_id=scope_data.tenant_id,
        knowledge_base_id=scope_data.primary_kb_id,
        created_by_user_id=scope_data.user_id,
        source_id="c" * 64,
        file_name="待索引.txt",
        storage_uri=final_path.resolve().as_uri(),
        mime_type="text/plain",
        content_hash="d" * 64,
        status="pending",
        version=1,
        metadata_json={},
    )
    session.add(document)
    session.flush()
    job = DocumentIndexingJob(
        tenant_id=scope_data.tenant_id,
        knowledge_base_id=scope_data.primary_kb_id,
        document_id=document.id,
        requested_by_user_id=scope_data.user_id,
        staged_storage_uri=(
            candidate_path.resolve().as_uri()
        ),
        candidate_file_name="待索引.txt",
        candidate_mime_type="text/plain",
        candidate_content_hash="d" * 64,
        target_version=1,
        status="queued",
        attempt_count=0,
        max_attempts=3,
    )
    session.add(job)
    session.commit()
    document_id = document.id
    job_id = job.id

    DocumentDeletionService(
        session=session,
        storage_dir=storage_dir,
    ).delete(
        scope=scope_data.primary_scope,
        document_id=document_id,
    )

    assert candidate_path.exists() is False
    assert staging_directory.exists() is False
    assert session.get(KnowledgeDocument, document_id) is None
    assert session.get(DocumentIndexingJob, job_id) is None


def test_does_not_delete_document_outside_scope(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "documents"
    other_file = (
        storage_dir
        / str(scope_data.tenant_id)
        / str(scope_data.other_kb_id)
        / "other.txt"
    )
    other_file.parent.mkdir(parents=True)
    other_file.write_text("other", encoding="utf-8")
    document = _create_document(
        session,
        scope=scope_data.other_scope,
        user_id=scope_data.user_id,
        storage_uri=other_file.as_uri(),
    )
    document_id = document.id

    service = DocumentDeletionService(
        session=session,
        storage_dir=storage_dir,
    )

    with pytest.raises(
        DocumentNotFoundError,
        match="文档不存在",
    ):
        service.delete(
            scope=scope_data.primary_scope,
            document_id=document_id,
        )

    assert other_file.read_text(encoding="utf-8") == "other"
    assert _document_count(session, document_id) == 1
    assert _chunk_count(session, document_id) == 1


def test_preserves_source_file_outside_managed_storage(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
) -> None:
    storage_dir = tmp_path / "documents"
    external_file = (
        tmp_path
        / "sample_docs"
        / "original.txt"
    )
    external_file.parent.mkdir(parents=True)
    external_file.write_text(
        "命令行入库的原始文件",
        encoding="utf-8",
    )
    document = _create_document(
        session,
        scope=scope_data.primary_scope,
        user_id=scope_data.user_id,
        storage_uri=external_file.as_uri(),
    )
    document_id = document.id

    service = DocumentDeletionService(
        session=session,
        storage_dir=storage_dir,
    )
    service.delete(
        scope=scope_data.primary_scope,
        document_id=document_id,
    )

    assert external_file.read_text(encoding="utf-8") == (
        "命令行入库的原始文件"
    )
    assert _document_count(session, document_id) == 0
    assert _chunk_count(session, document_id) == 0


def test_restores_managed_file_when_database_commit_fails(
    session: Session,
    scope_data: ScopeData,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_dir = tmp_path / "documents"
    managed_file = (
        storage_dir
        / str(scope_data.tenant_id)
        / str(scope_data.primary_kb_id)
        / "rollback.txt"
    )
    managed_file.parent.mkdir(parents=True)
    managed_file.write_text(
        "必须恢复的原始内容",
        encoding="utf-8",
    )
    document = _create_document(
        session,
        scope=scope_data.primary_scope,
        user_id=scope_data.user_id,
        storage_uri=managed_file.as_uri(),
    )
    document_id = document.id

    def fail_commit() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        session,
        "commit",
        fail_commit,
    )
    service = DocumentDeletionService(
        session=session,
        storage_dir=storage_dir,
    )

    with pytest.raises(
        RuntimeError,
        match="database unavailable",
    ):
        service.delete(
            scope=scope_data.primary_scope,
            document_id=document_id,
        )

    assert managed_file.read_text(encoding="utf-8") == (
        "必须恢复的原始内容"
    )
    assert _document_count(session, document_id) == 1
    assert _chunk_count(session, document_id) == 1
    assert _staged_files(managed_file.parent) == []
