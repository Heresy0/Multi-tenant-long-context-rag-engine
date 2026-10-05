from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    KnowledgeDocument,
)
from ..security.retrieval_scope import RetrievalScope
from ..documents.policy_metadata import extract_policy


@dataclass(frozen=True, slots=True)
class StoredChunk:
    """当前知识库中的一个可检索分块。"""

    chunk_id: str
    document_id: UUID
    tenant_id: UUID
    knowledge_base_id: UUID
    content: str
    document_name: str
    source: str
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class VectorSearchHit(StoredChunk):
    """一次向量检索返回的文档分块。"""

    distance: float


class PgVectorRepository:
    """在授权范围内执行pgvector查询。"""

    def __init__(
        self,
        session: Session,
    ) -> None:
        self._session = session

    @staticmethod
    def _build_metadata(
        *,
        chunk: DocumentChunk,
        document: KnowledgeDocument,
    ) -> dict[str, object]:
        metadata = dict(chunk.metadata_json or {})
        stored_policy = (document.metadata_json or {}).get("policy")
        if isinstance(stored_policy, dict):
            metadata["policy"] = stored_policy

        metadata.update({
            "tenant_id": str(chunk.tenant_id),
            "knowledge_base_id": str(
                chunk.knowledge_base_id
            ),
            "document_id": str(document.id),
            "source_id": document.source_id,
            "chunk_id": chunk.id,
            "chunk_index": chunk.chunk_index,
            "chunk_content_hash": chunk.content_hash,
            "document_name": document.file_name,
            "source": document.storage_uri,
        })

        return metadata

    def candidate_policies(self, *, scope, document_ids):
        """Read headers of candidate documents only, with scope/ready guards.

        Read at most the first eight chunks per candidate. Legacy indexes need no
        embedding rebuild. Extracted policy is request-local;
        this method never persists metadata or loads the whole knowledge base.
        """
        if not document_ids:
            return {}
        rows = self._session.execute(select(KnowledgeDocument, DocumentChunk).join(
            DocumentChunk, and_(KnowledgeDocument.id == DocumentChunk.document_id,
                KnowledgeDocument.tenant_id == DocumentChunk.tenant_id,
                KnowledgeDocument.knowledge_base_id == DocumentChunk.knowledge_base_id),
        ).where(KnowledgeDocument.id.in_(document_ids), KnowledgeDocument.tenant_id == scope.tenant_id,
                scope.knowledge_base_filter(KnowledgeDocument.knowledge_base_id),
                KnowledgeDocument.status == "ready", DocumentChunk.chunk_index < 8
                ).order_by(KnowledgeDocument.id, DocumentChunk.chunk_index)).all()
        grouped = {}
        for document, chunk in rows:
            grouped.setdefault(str(document.id), (document, []))[1].append(chunk.content)
        return {identifier: {**extract_policy("\n".join(contents), doc.file_name),
                            **((doc.metadata_json or {}).get("policy") if isinstance((doc.metadata_json or {}).get("policy"), dict) else {})}
                for identifier, (doc, contents) in grouped.items()}

    def candidate_companions(self, *, scope, document_ids):
        """First eight chunks of at most two already matched documents, scoped."""
        ids = list(document_ids)
        if not ids:
            return []
        if len(ids) > 2:
            raise ValueError("正文补取最多允许两份候选文档")
        rows = self._session.execute(select(DocumentChunk, KnowledgeDocument).join(
            KnowledgeDocument, and_(KnowledgeDocument.id == DocumentChunk.document_id,
                KnowledgeDocument.tenant_id == DocumentChunk.tenant_id,
                KnowledgeDocument.knowledge_base_id == DocumentChunk.knowledge_base_id),
        ).where(DocumentChunk.document_id.in_(ids), DocumentChunk.tenant_id == scope.tenant_id,
                scope.knowledge_base_filter(DocumentChunk.knowledge_base_id), KnowledgeDocument.status == 'ready',
                DocumentChunk.chunk_index >= 0, DocumentChunk.chunk_index < 8,
        ).order_by(KnowledgeDocument.id, DocumentChunk.chunk_index).limit(16)).all()
        return [StoredChunk(chunk_id=chunk.id, document_id=document.id, tenant_id=chunk.tenant_id,
                            knowledge_base_id=chunk.knowledge_base_id, content=chunk.content,
                            document_name=document.file_name, source=document.storage_uri,
                            metadata=self._build_metadata(chunk=chunk, document=document))
                for chunk, document in rows]

    def search(
        self,
        *,
        scope: RetrievalScope,
        query_embedding: Sequence[float],
        limit: int = 30,
    ) -> list[VectorSearchHit]:
        query_vector = self._validated_query_vector(
            query_embedding
        )

        if not 1 <= limit <= 100:
            raise ValueError(
                "向量检索limit必须在1到100之间"
            )

        distance_expression = (
            DocumentChunk.embedding.cosine_distance(
                query_vector
            ).label("distance")
        )

        statement = (
            select(
                DocumentChunk,
                KnowledgeDocument,
                distance_expression,
            )
            .join(
                KnowledgeDocument,
                and_(
                    KnowledgeDocument.tenant_id
                    == DocumentChunk.tenant_id,
                    KnowledgeDocument.knowledge_base_id
                    == DocumentChunk.knowledge_base_id,
                    KnowledgeDocument.id
                    == DocumentChunk.document_id,
                ),
            )
            .where(
                DocumentChunk.tenant_id
                == scope.tenant_id,
                scope.knowledge_base_filter(DocumentChunk.knowledge_base_id),
                KnowledgeDocument.status == "ready",
            )
            .order_by(distance_expression)
            .limit(limit)
        )

        rows = self._session.execute(
            statement
        ).all()

        hits: list[VectorSearchHit] = []

        for chunk, document, distance in rows:
            metadata = self._build_metadata(
                chunk=chunk,
                document=document,
            )

            hits.append(
                VectorSearchHit(
                    chunk_id=chunk.id,
                    document_id=document.id,
                    tenant_id=chunk.tenant_id,
                    knowledge_base_id=(
                        chunk.knowledge_base_id
                    ),
                    content=chunk.content,
                    document_name=document.file_name,
                    source=document.storage_uri,
                    metadata=metadata,
                    distance=float(distance),
                )
            )

        return hits

    def has_ready_chunks(self, *, scope: RetrievalScope) -> bool:
        """Empty-scope check without loading the entire knowledge base."""
        statement = select(DocumentChunk.id).join(KnowledgeDocument, and_(
            KnowledgeDocument.tenant_id == DocumentChunk.tenant_id,
            KnowledgeDocument.knowledge_base_id == DocumentChunk.knowledge_base_id,
            KnowledgeDocument.id == DocumentChunk.document_id,
        )).where(
            DocumentChunk.tenant_id == scope.tenant_id,
            scope.knowledge_base_filter(DocumentChunk.knowledge_base_id),
            KnowledgeDocument.status == "ready",
        ).limit(1)
        return self._session.scalar(statement) is not None

    def list_chunks(
        self,
        *,
        scope: RetrievalScope,
    ) -> list[StoredChunk]:
        """列出授权范围内全部可用于关键词检索的分块。"""
        statement = (
            select(
                DocumentChunk,
                KnowledgeDocument,
            )
            .join(
                KnowledgeDocument,
                and_(
                    KnowledgeDocument.tenant_id
                    == DocumentChunk.tenant_id,
                    KnowledgeDocument.knowledge_base_id
                    == DocumentChunk.knowledge_base_id,
                    KnowledgeDocument.id
                    == DocumentChunk.document_id,
                ),
            )
            .where(
                DocumentChunk.tenant_id
                == scope.tenant_id,
                scope.knowledge_base_filter(DocumentChunk.knowledge_base_id),
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                scope.knowledge_base_filter(KnowledgeDocument.knowledge_base_id),
                KnowledgeDocument.status == "ready",
            )
            .order_by(
                KnowledgeDocument.id,
                DocumentChunk.chunk_index,
            )
        )

        rows = self._session.execute(
            statement
        ).all()

        chunks: list[StoredChunk] = []

        for chunk, document in rows:
            chunks.append(
                StoredChunk(
                    chunk_id=chunk.id,
                    document_id=document.id,
                    tenant_id=chunk.tenant_id,
                    knowledge_base_id=(
                        chunk.knowledge_base_id
                    ),
                    content=chunk.content,
                    document_name=document.file_name,
                    source=document.storage_uri,
                    metadata=self._build_metadata(
                        chunk=chunk,
                        document=document,
                    ),
                )
            )

        return chunks

    @staticmethod
    def _validated_query_vector(
        values: Sequence[float],
    ) -> list[float]:
        if len(values) != EMBEDDING_DIMENSION:
            raise ValueError(
                "查询向量维度必须为"
                f"{EMBEDDING_DIMENSION}"
            )

        result: list[float] = []

        for value in values:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
            ):
                raise ValueError(
                    "查询向量只能包含有限数值"
                )

            result.append(float(value))

        return result
