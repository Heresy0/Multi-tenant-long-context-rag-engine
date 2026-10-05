from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from langchain_core.embeddings import Embeddings
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.config import Settings
from backend.app.db.base import Base
from backend.app.db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
    User,
)
from backend.app.indexing.pgvector_indexing_service import (
    DocumentCandidateNotFound,
    PgVectorIndexingService,
)
from backend.app.security.retrieval_scope import RetrievalScope


class FakeEmbeddings(Embeddings):
    def __init__(self) -> None:
        self.document_calls = 0

    def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.document_calls += 1
        return [
            [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)
            for _text in texts
        ]

    def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0] + [0.0] * (
            EMBEDDING_DIMENSION - 1
        )


@dataclass(frozen=True, slots=True)
class Scenario:
    tenant_id: UUID
    user_id: UUID
    technology_scope: RetrievalScope
    hr_scope: RetrievalScope


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
    technology_kb_id = uuid4()
    hr_kb_id = uuid4()

    session.add(
        Tenant(
            id=tenant_id,
            name="候选文件索引测试企业",
        )
    )
    session.flush()
    session.add_all([
        User(
            id=user_id,
            tenant_id=tenant_id,
            external_subject="candidate-index-user",
            name="Candidate Index User",
        ),
        KnowledgeBase(
            id=technology_kb_id,
            tenant_id=tenant_id,
            name="技术部知识库",
            visibility="restricted",
        ),
        KnowledgeBase(
            id=hr_kb_id,
            tenant_id=tenant_id,
            name="人力资源部知识库",
            visibility="restricted",
        ),
    ])
    session.commit()

    return Scenario(
        tenant_id=tenant_id,
        user_id=user_id,
        technology_scope=RetrievalScope(
            tenant_id=tenant_id,
            knowledge_base_id=technology_kb_id,
        ),
        hr_scope=RetrievalScope(
            tenant_id=tenant_id,
            knowledge_base_id=hr_kb_id,
        ),
    )


def _settings() -> Settings:
    settings = object.__new__(Settings)
    settings.embedding_model = "text-embedding-v4"
    return settings


def _add_document(
    session: Session,
    scenario: Scenario,
    *,
    file_name: str,
    storage_uri: str,
    content_hash: str,
    status: str,
    version: int,
    marker: str,
) -> KnowledgeDocument:
    document = KnowledgeDocument(
        tenant_id=scenario.tenant_id,
        knowledge_base_id=(
            scenario.technology_scope.knowledge_base_id
        ),
        created_by_user_id=scenario.user_id,
        source_id=marker * 64,
        file_name=file_name,
        storage_uri=storage_uri,
        mime_type="text/plain",
        content_hash=content_hash,
        status=status,
        version=version,
        metadata_json={},
    )
    session.add(document)
    session.flush()
    return document


def _add_old_chunk(
    session: Session,
    document: KnowledgeDocument,
) -> str:
    chunk_id = sha256(b"old-chunk").hexdigest()
    session.add(
        DocumentChunk(
            id=chunk_id,
            tenant_id=document.tenant_id,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            chunk_index=0,
            content="旧版本内容",
            content_hash=sha256(
                "旧版本内容".encode("utf-8")
            ).hexdigest(),
            parent_id=None,
            chunking_version="structured-v1",
            embedding_model="text-embedding-v4",
            embedding=[0.0] * EMBEDDING_DIMENSION,
            metadata_json={},
        )
    )
    document.metadata_json = {
        "chunk_count": 1,
        "chunking_version": "structured-v1",
        "embedding_model": "text-embedding-v4",
    }
    session.commit()
    return chunk_id


def _write_candidate(
    tmp_path: Path,
    content: str,
) -> tuple[Path, str]:
    path = tmp_path / f"{uuid4().hex}.txt"
    data = content.encode("utf-8")
    path.write_bytes(data)
    return path, sha256(data).hexdigest()


def test_review_revision_change_reindexes_same_file_and_version_once(session, scenario, tmp_path, monkeypatch):
    from backend.app.indexing import pgvector_indexing_service as module
    path, digest = _write_candidate(tmp_path, '经过核查的示例内容。')
    uri = path.resolve().as_uri()
    document = _add_document(session, scenario, file_name='review.txt', storage_uri=uri,
                             content_hash=digest, status='pending', version=1, marker='r')
    session.commit()
    embeddings = FakeEmbeddings()
    service = PgVectorIndexingService(session=session, settings=_settings(), embeddings=embeddings)
    args = dict(file_path=path, scope=scenario.technology_scope, document_id=document.id,
                created_by_user_id=scenario.user_id, target_version=1, candidate_file_name='review.txt',
                candidate_mime_type='text/plain', candidate_content_hash=digest, final_storage_uri=uri)
    assert service.index_document_candidate(**args) > 0
    monkeypatch.setattr(module, 'review_revision', lambda _: 'review-r2')
    assert service.index_document_candidate(**args) > 0
    assert service.index_document_candidate(**args) == 0
    assert embeddings.document_calls == 2
    session.refresh(document)
    assert document.version == 1 and document.content_hash == digest
    assert document.metadata_json['ocr_review_revision'] == 'review-r2'


def test_indexes_pending_document_at_version_one_and_is_idempotent(
    session: Session,
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    path, candidate_hash = _write_candidate(
        tmp_path,
        "版本: 1.4\n生效日期: 2026年7月1日\n异步上传的新文档内容。",
    )
    final_uri = (
        tmp_path / "documents" / "研发规范.txt"
    ).resolve().as_uri()
    document = _add_document(
        session,
        scenario,
        file_name="研发规范.txt",
        storage_uri=final_uri,
        content_hash=candidate_hash,
        status="pending",
        version=1,
        marker="a",
    )
    session.commit()
    embeddings = FakeEmbeddings()
    service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=embeddings,
    )

    first_count = service.index_document_candidate(
        file_path=path,
        scope=scenario.technology_scope,
        document_id=document.id,
        created_by_user_id=scenario.user_id,
        target_version=1,
        candidate_file_name="研发规范.txt",
        candidate_mime_type="text/plain",
        candidate_content_hash=candidate_hash,
        final_storage_uri=final_uri,
    )
    second_count = service.index_document_candidate(
        file_path=path,
        scope=scenario.technology_scope,
        document_id=document.id,
        created_by_user_id=scenario.user_id,
        target_version=1,
        candidate_file_name="研发规范.txt",
        candidate_mime_type="text/plain",
        candidate_content_hash=candidate_hash,
        final_storage_uri=final_uri,
    )

    session.expire_all()
    stored = session.get(KnowledgeDocument, document.id)
    chunks = session.scalars(
        select(DocumentChunk).where(
            DocumentChunk.document_id == document.id
        )
    ).all()

    assert stored is not None
    assert stored.status == "ready"
    assert stored.version == 1
    assert stored.metadata_json["policy"]["business_version"] == "1.4"
    assert stored.metadata_json["policy"]["effective_from"] == "2026-07-01"
    assert stored.metadata_json["quality"]["review_required"] is False
    assert first_count == len(chunks)
    assert first_count >= 1
    assert second_count == 0
    assert embeddings.document_calls == 1
    assert all(
        chunk.metadata_json["source"] == final_uri
        and chunk.metadata_json["cleaning_version"] == "conservative-v1"
        and chunk.metadata_json["quality"]["review_required"] is False
        and chunk.metadata_json["policy"]["business_version"] == "1.4"
        and chunk.metadata_json["document_name"]
        == "研发规范"
        for chunk in chunks
    )


def test_replaces_existing_chunks_at_exact_target_version(
    session: Session,
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    path, candidate_hash = _write_candidate(
        tmp_path,
        "第二版研发规范内容。",
    )
    final_uri = (
        tmp_path / "documents" / "研发规范.txt"
    ).resolve().as_uri()
    document = _add_document(
        session,
        scenario,
        file_name="旧名称.txt",
        storage_uri="file:///documents/old.txt",
        content_hash="b" * 64,
        status="ready",
        version=1,
        marker="b",
    )
    old_chunk_id = _add_old_chunk(session, document)
    service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=FakeEmbeddings(),
    )

    count = service.index_document_candidate(
        file_path=path,
        scope=scenario.technology_scope,
        document_id=document.id,
        created_by_user_id=scenario.user_id,
        target_version=2,
        candidate_file_name="研发规范.txt",
        candidate_mime_type="text/plain",
        candidate_content_hash=candidate_hash,
        final_storage_uri=final_uri,
    )

    session.expire_all()
    stored = session.get(KnowledgeDocument, document.id)
    chunk_ids = set(
        session.scalars(
            select(DocumentChunk.id).where(
                DocumentChunk.document_id
                == document.id
            )
        ).all()
    )

    assert stored is not None
    assert stored.version == 2
    assert stored.file_name == "研发规范.txt"
    assert stored.content_hash == candidate_hash
    assert stored.storage_uri == final_uri
    assert len(chunk_ids) == count
    assert old_chunk_id not in chunk_ids


def test_hash_mismatch_preserves_existing_document_and_chunks(
    session: Session,
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    path, _candidate_hash = _write_candidate(
        tmp_path,
        "候选文件实际内容。",
    )
    document = _add_document(
        session,
        scenario,
        file_name="稳定版本.txt",
        storage_uri="file:///documents/stable.txt",
        content_hash="c" * 64,
        status="ready",
        version=1,
        marker="c",
    )
    old_chunk_id = _add_old_chunk(session, document)
    embeddings = FakeEmbeddings()
    service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=embeddings,
    )

    with pytest.raises(
        ValueError,
        match="暂存文件内容哈希不匹配",
    ):
        service.index_document_candidate(
            file_path=path,
            scope=scenario.technology_scope,
            document_id=document.id,
            created_by_user_id=scenario.user_id,
            target_version=2,
            candidate_file_name="新版.txt",
            candidate_mime_type="text/plain",
            candidate_content_hash="d" * 64,
            final_storage_uri=(
                "file:///documents/new.txt"
            ),
        )

    session.expire_all()
    stored = session.get(KnowledgeDocument, document.id)
    chunk_ids = set(
        session.scalars(
            select(DocumentChunk.id).where(
                DocumentChunk.document_id
                == document.id
            )
        ).all()
    )

    assert stored is not None
    assert stored.version == 1
    assert stored.content_hash == "c" * 64
    assert chunk_ids == {old_chunk_id}
    assert embeddings.document_calls == 0


def test_document_lookup_is_limited_to_retrieval_scope(
    session: Session,
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    path, candidate_hash = _write_candidate(
        tmp_path,
        "不能跨知识库索引。",
    )
    document = _add_document(
        session,
        scenario,
        file_name="隔离测试.txt",
        storage_uri="file:///documents/scoped.txt",
        content_hash="e" * 64,
        status="ready",
        version=1,
        marker="e",
    )
    session.commit()
    embeddings = FakeEmbeddings()
    service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=embeddings,
    )

    with pytest.raises(
        DocumentCandidateNotFound,
        match="待索引文档不存在",
    ):
        service.index_document_candidate(
            file_path=path,
            scope=scenario.hr_scope,
            document_id=document.id,
            created_by_user_id=scenario.user_id,
            target_version=2,
            candidate_file_name="隔离测试.txt",
            candidate_mime_type="text/plain",
            candidate_content_hash=candidate_hash,
            final_storage_uri=(
                "file:///documents/scoped.txt"
            ),
        )

    assert embeddings.document_calls == 0
