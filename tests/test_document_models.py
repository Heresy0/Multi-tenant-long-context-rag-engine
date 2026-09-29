from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    KnowledgeBase,
    KnowledgeDocument,
    Tenant,
    User,
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


def test_stores_document_chunk_in_kb_scope(
    session: Session,
) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    knowledge_base_id = uuid4()
    document_id = uuid4()

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
            id=knowledge_base_id,
            tenant_id=tenant_id,
            name="技术部知识库",
            visibility="restricted",
        ),
    ])
    session.flush()

    session.add(
        KnowledgeDocument(
            id=document_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            created_by_user_id=user_id,
            source_id="a" * 64,
            file_name="研发规范.pdf",
            storage_uri="oss://documents/研发规范.pdf",
            mime_type="application/pdf",
            content_hash="b" * 64,
            status="ready",
        )
    )
    session.flush()

    session.add(
        DocumentChunk(
            id="c" * 64,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            chunk_index=0,
            content="这是技术部研发规范。",
            content_hash="d" * 64,
            chunking_version="structured-v1",
            embedding_model="text-embedding-v4",
            embedding=(
                [1.0]
                + [0.0] * (EMBEDDING_DIMENSION - 1)
            ),
        )
    )
    session.commit()

    stored_chunk = session.get(
        DocumentChunk,
        "c" * 64,
    )

    assert stored_chunk is not None
    assert stored_chunk.tenant_id == tenant_id
    assert stored_chunk.knowledge_base_id == knowledge_base_id
    assert stored_chunk.document_id == document_id
    assert len(stored_chunk.embedding) == EMBEDDING_DIMENSION


def _add_tenant(
    session: Session,
    name: str,
) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name=name,
    )
    session.add(tenant)
    session.flush()
    return tenant


def _add_knowledge_base(
    session: Session,
    tenant_id,
    name: str,
) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(
        id=uuid4(),
        tenant_id=tenant_id,
        name=name,
        visibility="restricted",
    )
    session.add(knowledge_base)
    session.flush()
    return knowledge_base


def _new_document(
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    source_character: str,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        created_by_user_id=None,
        source_id=source_character * 64,
        file_name=f"{source_character}.txt",
        storage_uri=f"oss://documents/{source_character}.txt",
        mime_type="text/plain",
        content_hash=source_character * 64,
        status="ready",
    )


def _new_chunk(
    *,
    chunk_id: str,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
) -> DocumentChunk:
    return DocumentChunk(
        id=chunk_id * 64,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        chunk_index=0,
        content="用于权限隔离测试的内容。",
        content_hash="f" * 64,
        chunking_version="structured-v1",
        embedding_model="text-embedding-v4",
        embedding=(
            [1.0]
            + [0.0] * (EMBEDDING_DIMENSION - 1)
        ),
    )


def test_rejects_cross_tenant_document_scope(
    session: Session,
) -> None:
    tenant_a = _add_tenant(session, "企业A")
    tenant_b = _add_tenant(session, "企业B")

    knowledge_base_b = _add_knowledge_base(
        session,
        tenant_b.id,
        "企业B知识库",
    )

    document = _new_document(
        tenant_id=tenant_a.id,
        knowledge_base_id=knowledge_base_b.id,
        source_character="e",
    )
    session.add(document)

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()


def test_rejects_chunk_with_wrong_knowledge_base(
    session: Session,
) -> None:
    tenant = _add_tenant(session, "星海科技有限公司")

    technology_kb = _add_knowledge_base(
        session,
        tenant.id,
        "技术部知识库",
    )
    hr_kb = _add_knowledge_base(
        session,
        tenant.id,
        "人力资源部知识库",
    )

    document = _new_document(
        tenant_id=tenant.id,
        knowledge_base_id=technology_kb.id,
        source_character="g",
    )
    session.add(document)
    session.flush()

    wrong_chunk = _new_chunk(
        chunk_id="h",
        tenant_id=tenant.id,
        knowledge_base_id=hr_kb.id,
        document_id=document.id,
    )
    session.add(wrong_chunk)

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()


def test_deleting_document_deletes_its_chunks(
    session: Session,
) -> None:
    tenant = _add_tenant(session, "星海科技有限公司")
    knowledge_base = _add_knowledge_base(
        session,
        tenant.id,
        "技术部知识库",
    )

    document = _new_document(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge_base.id,
        source_character="i",
    )
    session.add(document)
    session.flush()

    chunk = _new_chunk(
        chunk_id="j",
        tenant_id=tenant.id,
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
    )
    session.add(chunk)
    session.commit()

    chunk_id = chunk.id

    session.delete(document)
    session.commit()
    session.expire_all()

    assert session.get(DocumentChunk, chunk_id) is None