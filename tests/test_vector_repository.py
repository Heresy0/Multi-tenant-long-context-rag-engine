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
from backend.app.knowledge.vector_repository import (
    PgVectorRepository,
)


def _valid_embedding() -> list[float]:
    return (
        [1.0]
        + [0.0] * (EMBEDDING_DIMENSION - 1)
    )


def test_companion_read_is_bounded_by_scope_ready_candidate_ids_and_chunk_count():
    repository, session = _repository()
    scope, doc_id = RetrievalScope(tenant_id=uuid4(), knowledge_base_id=uuid4()), uuid4()
    assert repository.candidate_companions(scope=scope, document_ids=[doc_id]) == []
    sql = str(session.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    for expected in (str(scope.tenant_id), str(scope.knowledge_base_id), str(doc_id), "documents.status = 'ready'", 'chunk_index < 8', 'LIMIT 16'):
        assert expected in sql
    assert sql.index('WHERE') < sql.index('LIMIT')
    with pytest.raises(ValueError):
        repository.candidate_companions(scope=scope, document_ids=[uuid4(), uuid4(), uuid4()])


@pytest.mark.parametrize("ids", [(), (uuid4(), uuid4())])
def test_multi_scope_filters_before_limit_and_empty_scope_is_never_unrestricted(ids):
    repository, session = _repository()
    scope = RetrievalScope(tenant_id=uuid4(), knowledge_base_ids=ids)
    repository.search(scope=scope, query_embedding=_valid_embedding(), limit=8)
    statement = session.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "document_chunks.knowledge_base_id IN" in sql
    assert str(scope.tenant_id) in sql
    assert "documents.status = 'ready'" in sql
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
    for kb_id in ids:
        assert str(kb_id) in sql
    if not ids:
        assert "1 != 1" in sql


def _repository(
    rows: list[tuple] | None = None,
) -> tuple[PgVectorRepository, Mock]:
    session = Mock(spec=Session)
    session.execute.return_value.all.return_value = (
        rows or []
    )

    return PgVectorRepository(session), session


@pytest.mark.parametrize("chunk_id,exists", [(None, False), ("c" * 64, True)])
def test_ready_check_selects_only_one_id_in_the_authorized_scope(chunk_id, exists):
    repository, session = _repository()
    session.scalar.return_value = chunk_id
    scope = RetrievalScope(tenant_id=uuid4(), knowledge_base_id=uuid4())
    assert repository.has_ready_chunks(scope=scope) is exists
    statement = session.scalar.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert compiled.startswith("SELECT document_chunks.id")
    assert "document_chunks.content" not in compiled
    assert str(scope.tenant_id) in compiled
    assert str(scope.knowledge_base_id) in compiled
    assert "documents.status = 'ready'" in compiled
    assert "LIMIT 1" in compiled
    session.execute.assert_not_called()


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
    assert hit.metadata["source_id"] == document.source_id
    assert (
        hit.metadata["chunk_content_hash"]
        == chunk.content_hash
    )
    assert (
        hit.metadata["knowledge_base_id"]
        == str(knowledge_base_id)
    )


def test_list_chunks_is_scoped_and_maps_ready_documents() -> None:
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    document_id = uuid4()
    document = KnowledgeDocument(
        id=document_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        created_by_user_id=None,
        source_id="a" * 64,
        file_name="开放平台手册.docx",
        storage_uri="file:///开放平台手册.docx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
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
        chunk_index=2,
        content="API故障处理内容",
        content_hash="d" * 64,
        parent_id=None,
        chunking_version="structured-v1",
        embedding_model="text-embedding-v4",
        embedding=_valid_embedding(),
        metadata_json={"section_path": "故障处理"},
    )
    repository, session = _repository([
        (chunk, document),
    ])
    scope = RetrievalScope(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    results = repository.list_chunks(scope=scope)

    assert len(results) == 1
    stored = results[0]
    assert stored.chunk_id == chunk.id
    assert stored.content == chunk.content
    assert stored.document_name == document.file_name
    assert stored.metadata["source_id"] == document.source_id
    assert stored.metadata["section_path"] == "故障处理"

    statement = session.execute.call_args.args[0]
    compiled = statement.compile(
        dialect=postgresql.dialect()
    )
    sql = str(compiled)

    assert "document_chunks.tenant_id =" in sql
    assert "document_chunks.knowledge_base_id =" in sql
    assert "documents.tenant_id =" in sql
    assert "documents.knowledge_base_id =" in sql
    assert "documents.status =" in sql
    assert scope.tenant_id in compiled.params.values()
    assert scope.knowledge_base_id in compiled.params.values()
