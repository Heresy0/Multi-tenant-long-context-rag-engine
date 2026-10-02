from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from langchain_core.embeddings import Embeddings
from sqlalchemy import (
    Engine,
    create_engine,
    event,
    func,
    select,
)
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
    PgVectorIndexingService,
)
from backend.app.security.retrieval_scope import (
    RetrievalScope,
)


class FakeEmbeddings(Embeddings):
    def __init__(self) -> None:
        self.document_calls = 0

    def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.document_calls += 1
        results: list[list[float]] = []

        for index, _text in enumerate(texts):
            vector = [0.0] * EMBEDDING_DIMENSION
            vector[index % 2] = 1.0
            results.append(vector)

        return results

    def embed_query(
        self,
        text: str,
    ) -> list[float]:
        del text
        vector = [0.0] * EMBEDDING_DIMENSION
        vector[0] = 1.0
        return vector


class WrongDimensionEmbeddings(FakeEmbeddings):
    def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.document_calls += 1
        return [
            [1.0]
            for _text in texts
        ]


@dataclass(frozen=True, slots=True)
class ScopeData:
    tenant_id: UUID
    user_id: UUID
    technology_kb_id: UUID
    hr_kb_id: UUID


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
def scope_data(
    session: Session,
) -> ScopeData:
    tenant_id = uuid4()
    user_id = uuid4()
    technology_kb_id = uuid4()
    hr_kb_id = uuid4()

    session.add(
        Tenant(
            id=tenant_id,
            name="星海科技有限公司",
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

    return ScopeData(
        tenant_id=tenant_id,
        user_id=user_id,
        technology_kb_id=technology_kb_id,
        hr_kb_id=hr_kb_id,
    )


def _settings() -> Settings:
    settings = object.__new__(Settings)
    settings.embedding_model = "text-embedding-v4"
    return settings


def _count(
    session: Session,
    model,
) -> int:
    return session.scalar(
        select(func.count()).select_from(model)
    ) or 0


def test_indexes_file_and_skips_unchanged_file(
    session: Session,
    scope_data: ScopeData,
    tmp_path,
) -> None:
    file_path = tmp_path / "研发规范.txt"
    file_path.write_text(
        "星海科技研发规范测试内容。",
        encoding="utf-8",
    )

    embeddings = FakeEmbeddings()
    service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=embeddings,
    )
    scope = RetrievalScope(
        tenant_id=scope_data.tenant_id,
        knowledge_base_id=(
            scope_data.technology_kb_id
        ),
    )

    first_count = service.index_file(
        file_path=file_path,
        scope=scope,
        created_by_user_id=scope_data.user_id,
    )
    second_count = service.index_file(
        file_path=file_path,
        scope=scope,
        created_by_user_id=scope_data.user_id,
    )

    assert first_count >= 1
    assert second_count == 0
    assert embeddings.document_calls == 1
    assert _count(session, KnowledgeDocument) == 1
    assert _count(session, DocumentChunk) == first_count

    document = session.scalar(
        select(KnowledgeDocument)
    )

    assert document is not None
    assert document.status == "ready"
    assert document.file_size_bytes == file_path.stat().st_size
    assert (
        document.metadata_json["chunk_count"]
        == first_count
    )


def test_same_file_in_two_knowledge_bases_has_distinct_chunks(
    session: Session,
    scope_data: ScopeData,
    tmp_path,
) -> None:
    file_path = tmp_path / "公共资料.txt"
    file_path.write_text(
        "同一份资料进入两个不同知识库。",
        encoding="utf-8",
    )

    service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=FakeEmbeddings(),
    )

    technology_scope = RetrievalScope(
        tenant_id=scope_data.tenant_id,
        knowledge_base_id=(
            scope_data.technology_kb_id
        ),
    )
    hr_scope = RetrievalScope(
        tenant_id=scope_data.tenant_id,
        knowledge_base_id=scope_data.hr_kb_id,
    )

    service.index_file(
        file_path=file_path,
        scope=technology_scope,
        created_by_user_id=scope_data.user_id,
    )
    service.index_file(
        file_path=file_path,
        scope=hr_scope,
        created_by_user_id=scope_data.user_id,
    )

    technology_chunk_ids = set(
        session.scalars(
            select(DocumentChunk.id).where(
                DocumentChunk.knowledge_base_id
                == scope_data.technology_kb_id
            )
        ).all()
    )
    hr_chunk_ids = set(
        session.scalars(
            select(DocumentChunk.id).where(
                DocumentChunk.knowledge_base_id
                == scope_data.hr_kb_id
            )
        ).all()
    )

    assert technology_chunk_ids
    assert hr_chunk_ids
    assert technology_chunk_ids.isdisjoint(
        hr_chunk_ids
    )


def test_embedding_failure_preserves_previous_version(
    session: Session,
    scope_data: ScopeData,
    tmp_path,
) -> None:
    file_path = tmp_path / "稳定文档.txt"
    file_path.write_text(
        "这是已经成功入库的旧版本。",
        encoding="utf-8",
    )

    scope = RetrievalScope(
        tenant_id=scope_data.tenant_id,
        knowledge_base_id=(
            scope_data.technology_kb_id
        ),
    )

    good_service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=FakeEmbeddings(),
    )
    good_service.index_file(
        file_path=file_path,
        scope=scope,
        created_by_user_id=scope_data.user_id,
    )

    original_document = session.scalar(
        select(KnowledgeDocument)
    )
    assert original_document is not None

    original_hash = original_document.content_hash
    original_chunk_ids = set(
        session.scalars(
            select(DocumentChunk.id)
        ).all()
    )

    file_path.write_text(
        "这是准备更新的新版本。",
        encoding="utf-8",
    )

    failing_service = PgVectorIndexingService(
        session=session,
        settings=_settings(),
        embeddings=WrongDimensionEmbeddings(),
    )

    with pytest.raises(
        ValueError,
        match="文档向量维度",
    ):
        failing_service.index_file(
            file_path=file_path,
            scope=scope,
            created_by_user_id=scope_data.user_id,
        )

    session.expire_all()

    preserved_document = session.scalar(
        select(KnowledgeDocument)
    )
    preserved_chunk_ids = set(
        session.scalars(
            select(DocumentChunk.id)
        ).all()
    )

    assert preserved_document is not None
    assert preserved_document.content_hash == original_hash
    assert preserved_document.status == "ready"
    assert preserved_chunk_ids == original_chunk_ids
