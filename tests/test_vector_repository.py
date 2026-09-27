from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from backend.app.db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    KnowledgeDocument,
)
from backend.app.security.retrieval_scope import (
    RetrievalScope,
)
from backend.app.vector_repository import (
    PgVectorRepository,
)


def _valid_embedding() -> list[float]:
    return (
        [1.0]
        + [0.0] * (EMBEDDING_DIMENSION - 1)
    )


def _repository(
    rows: list[tuple[
        DocumentChunk,
        KnowledgeDocument,
        float,
    ]] | None = None,
) -> tuple[PgVectorRepository, Mock]:
    session = Mock(spec=Session)
    session.execute.return_value.all.return_value = (
        rows or []
    )

    return PgVectorRepository(session), session


def test_rejects_wrong_embedding_dimension() -> None:
    repository, session = _repository()
    scope = RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )

    with pytest.raises(
        ValueError,
        match="查询向量维度",
    ):
        repository.search(
            scope=scope,
            query_embedding=[0.0] * 10,
        )

    session.execute.assert_not_called()


@pytest.mark.parametrize("limit", [0, 101])
def test_rejects_invalid_limit(limit: int) -> None:
    repository, session = _repository()
    scope = RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )

    with pytest.raises(
        ValueError,
        match="limit",
    ):
        repository.search(
            scope=scope,
            query_embedding=_valid_embedding(),
            limit=limit,
        )

    session.execute.assert_not_called()


def test_rejects_non_finite_vector_value() -> None:
    repository, session = _repository()
    scope = RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )
    embedding = _valid_embedding()
    embedding[0] = float("nan")

    with pytest.raises(
        ValueError,
        match="有限数值",
    ):
        repository.search(
            scope=scope,
            query_embedding=embedding,
        )

    session.execute.assert_not_called()


def test_search_sql_contains_required_scope_filters() -> None:
    repository, session = _repository()
    scope = RetrievalScope(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
    )

    results = repository.search(
        scope=scope,
        query_embedding=_valid_embedding(),
        limit=8,
    )

    assert results == []

    statement = session.execute.call_args.args[0]
    compiled = statement.compile(
        dialect=postgresql.dialect()
    )
    sql = str(compiled)

    assert "document_chunks.tenant_id =" in sql
    assert (
        "document_chunks.knowledge_base_id ="
        in sql
    )
    assert "documents.status =" in sql
    assert scope.tenant_id in compiled.params.values()
    assert (
        scope.knowledge_base_id
        in compiled.params.values()
    )


def test_maps_database_rows_to_search_hits() -> None:
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    document_id = uuid4()

    document = KnowledgeDocument(
        id=document_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        created_by_user_id=None,
        source_id="a" * 64,
        file_name="研发规范.pdf",
        storage_uri="oss://documents/研发规范.pdf",
        mime_type="application/pdf",
        content_hash="b" * 64,
        status="ready",
        version=1,
        metadata_json={},
    )

    chunk = DocumentChunk(
        id="c" * 64,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        chunk_index=0,
        content="研发规范内容",
        content_hash="d" * 64,
        parent_id=None,
        chunking_version="structured-v1",
        embedding_model="text-embedding-v4",
        embedding=_valid_embedding(),
        metadata_json={
            "tenant_id": "伪造的租户",
            "custom": "保留",
        },
    )

    repository, _session = _repository([
        (chunk, document, 0.125),
    ])
    scope = RetrievalScope(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    results = repository.search(
        scope=scope,
        query_embedding=_valid_embedding(),
        limit=8,
    )

    assert len(results) == 1

    hit = results[0]

    assert hit.chunk_id == chunk.id
    assert hit.distance == 0.125
    assert hit.metadata["custom"] == "保留"
    assert hit.metadata["tenant_id"] == str(tenant_id)
    assert (
        hit.metadata["knowledge_base_id"]
        == str(knowledge_base_id)
    )