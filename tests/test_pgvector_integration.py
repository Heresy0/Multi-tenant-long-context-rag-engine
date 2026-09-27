import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import required_env
from backend.app.db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
)
from backend.app.security.retrieval_scope import (
    RetrievalScope,
)
from backend.app.vector_repository import (
    PgVectorRepository,
)


def _identifier() -> str:
    return uuid4().hex + uuid4().hex


def _embedding(
    first: float,
    second: float,
) -> list[float]:
    return (
        [first, second]
        + [0.0] * (EMBEDDING_DIMENSION - 2)
    )


@pytest.fixture
def postgres_session() -> Iterator[Session]:
    if (
        os.getenv("RUN_POSTGRES_INTEGRATION_TESTS")
        != "1"
    ):
        pytest.skip(
            "未启用PostgreSQL集成测试"
        )

    engine = create_engine(
        required_env("DATABASE_URL"),
        pool_pre_ping=True,
    )
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        expire_on_commit=False,
    )

    try:
        yield session
    finally:
        session.close()

        if transaction.is_active:
            transaction.rollback()

        connection.close()
        engine.dispose()


@pytest.mark.integration
def test_real_pgvector_search_is_scoped_and_ordered(
    postgres_session: Session,
) -> None:
    tenant_a = Tenant(
        id=uuid4(),
        name="pgvector测试企业A",
    )
    tenant_b = Tenant(
        id=uuid4(),
        name="pgvector测试企业B",
    )
    postgres_session.add_all([
        tenant_a,
        tenant_b,
    ])
    postgres_session.flush()

    knowledge_base_a = KnowledgeBase(
        id=uuid4(),
        tenant_id=tenant_a.id,
        name="测试知识库A",
        visibility="restricted",
    )
    knowledge_base_b = KnowledgeBase(
        id=uuid4(),
        tenant_id=tenant_b.id,
        name="测试知识库B",
        visibility="restricted",
    )
    postgres_session.add_all([
        knowledge_base_a,
        knowledge_base_b,
    ])
    postgres_session.flush()

    document_a = KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant_a.id,
        knowledge_base_id=knowledge_base_a.id,
        created_by_user_id=None,
        source_id=_identifier(),
        file_name="企业A文档.txt",
        storage_uri="test://tenant-a/document",
        mime_type="text/plain",
        content_hash=_identifier(),
        status="ready",
        version=1,
        metadata_json={},
    )
    document_b = KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant_b.id,
        knowledge_base_id=knowledge_base_b.id,
        created_by_user_id=None,
        source_id=_identifier(),
        file_name="企业B文档.txt",
        storage_uri="test://tenant-b/document",
        mime_type="text/plain",
        content_hash=_identifier(),
        status="ready",
        version=1,
        metadata_json={},
    )
    postgres_session.add_all([
        document_a,
        document_b,
    ])
    postgres_session.flush()

    near_chunk_id = _identifier()
    far_chunk_id = _identifier()
    other_tenant_chunk_id = _identifier()

    postgres_session.add_all([
        DocumentChunk(
            id=near_chunk_id,
            tenant_id=tenant_a.id,
            knowledge_base_id=knowledge_base_a.id,
            document_id=document_a.id,
            chunk_index=0,
            content="企业A最相关内容",
            content_hash=_identifier(),
            chunking_version="structured-v1",
            embedding_model="text-embedding-v4",
            embedding=_embedding(1.0, 0.0),
            metadata_json={},
        ),
        DocumentChunk(
            id=far_chunk_id,
            tenant_id=tenant_a.id,
            knowledge_base_id=knowledge_base_a.id,
            document_id=document_a.id,
            chunk_index=1,
            content="企业A较不相关内容",
            content_hash=_identifier(),
            chunking_version="structured-v1",
            embedding_model="text-embedding-v4",
            embedding=_embedding(0.0, 1.0),
            metadata_json={},
        ),
        DocumentChunk(
            id=other_tenant_chunk_id,
            tenant_id=tenant_b.id,
            knowledge_base_id=knowledge_base_b.id,
            document_id=document_b.id,
            chunk_index=0,
            content="企业B完全匹配但不可见",
            content_hash=_identifier(),
            chunking_version="structured-v1",
            embedding_model="text-embedding-v4",
            embedding=_embedding(1.0, 0.0),
            metadata_json={},
        ),
    ])
    postgres_session.flush()

    repository = PgVectorRepository(
        postgres_session
    )
    scope = RetrievalScope(
        tenant_id=tenant_a.id,
        knowledge_base_id=knowledge_base_a.id,
    )

    hits = repository.search(
        scope=scope,
        query_embedding=_embedding(1.0, 0.0),
        limit=10,
    )

    assert [
        hit.chunk_id
        for hit in hits
    ] == [
        near_chunk_id,
        far_chunk_id,
    ]

    assert all(
        hit.tenant_id == tenant_a.id
        for hit in hits
    )
    assert all(
        hit.knowledge_base_id
        == knowledge_base_a.id
        for hit in hits
    )
    assert other_tenant_chunk_id not in {
        hit.chunk_id
        for hit in hits
    }

    assert hits[0].distance == pytest.approx(
        0.0,
        abs=1e-6,
    )
    assert hits[1].distance == pytest.approx(
        1.0,
        abs=1e-6,
    )