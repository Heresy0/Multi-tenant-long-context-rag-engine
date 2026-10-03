"""PostgreSQL pg_search 0.25.11 keyword retrieval, using its v2 API."""
from dataclasses import dataclass

from sqlalchemy import Engine, and_, bindparam, func, select, text
from sqlalchemy.orm import Session

from ..db.models import DocumentChunk, KnowledgeDocument
from ..security.retrieval_scope import RetrievalScope
from .keyword_tokenizer import build_keyword_text
from .vector_repository import PgVectorRepository, StoredChunk

KEYWORD_INDEX_NAME = "ix_document_chunks_keyword_bm25"
PG_SEARCH_VERSION = "0.25.11"


@dataclass(frozen=True, slots=True)
class KeywordSearchHit(StoredChunk):
    score: float


class PgKeywordRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def search_statement(*, all_accessible=False):
        """Filter scope and ready documents before LIMIT; bind all user inputs."""
        score = func.pdb.score(DocumentChunk.id).label("bm25_score")
        return (
            select(DocumentChunk, KnowledgeDocument, score)
            .join(KnowledgeDocument, and_(
                KnowledgeDocument.tenant_id == DocumentChunk.tenant_id,
                KnowledgeDocument.knowledge_base_id == DocumentChunk.knowledge_base_id,
                KnowledgeDocument.id == DocumentChunk.document_id,
            ))
            .where(
                DocumentChunk.tenant_id == bindparam("tenant_id"),
                (DocumentChunk.knowledge_base_id.in_(bindparam("knowledge_base_ids", expanding=True))
                 if all_accessible else DocumentChunk.knowledge_base_id == bindparam("knowledge_base_id")),
                KnowledgeDocument.status == "ready",
                DocumentChunk.keyword_text.op("|||", is_comparison=True)(bindparam("keyword_query")),
            )
            .order_by(score.desc(), DocumentChunk.id.asc())
            .limit(bindparam("result_limit"))
        )

    def search(self, query: str, *, scope: RetrievalScope, limit: int = 30) -> list[KeywordSearchHit]:
        if not 1 <= limit <= 100:
            raise ValueError("关键词检索 limit 必须在 1 到 100 之间。")
        keyword_query = build_keyword_text(query)
        if not keyword_query:
            return []
        parameters = {
            "tenant_id": scope.tenant_id,
            "keyword_query": keyword_query,
            "result_limit": limit,
        }
        all_accessible = scope.knowledge_base_id is None
        if all_accessible:
            parameters["knowledge_base_ids"] = sorted(scope.knowledge_base_ids, key=str)
        else:
            parameters["knowledge_base_id"] = scope.knowledge_base_id
        rows = self._session.execute(self.search_statement(all_accessible=all_accessible), parameters).all()
        return [KeywordSearchHit(
            chunk_id=chunk.id, document_id=document.id,
            tenant_id=chunk.tenant_id, knowledge_base_id=chunk.knowledge_base_id,
            content=chunk.content, document_name=document.file_name, source=document.storage_uri,
            metadata=PgVectorRepository._build_metadata(chunk=chunk, document=document),
            score=float(score),
        ) for chunk, document, score in rows]


def verify_keyword_index(engine: Engine) -> None:
    """Fail startup with actionable instructions rather than silently losing BM25."""
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'pg_search'"))
        if version != PG_SEARCH_VERSION:
            raise RuntimeError(
                f"关键词检索需要 pg_search {PG_SEARCH_VERSION}；请先构建新的 PostgreSQL 镜像并执行迁移。"
            )
        valid = connection.scalar(text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = :name AND n.nspname = current_schema()"
        ), {"name": KEYWORD_INDEX_NAME})
        if not valid:
            raise RuntimeError("BM25 索引未就绪，请执行 python -m alembic upgrade head。")
        preloaded = connection.scalar(text("SHOW shared_preload_libraries"))
        if "pg_search" not in {name.strip() for name in str(preloaded).split(",")}:
            raise RuntimeError("PostgreSQL 必须通过 shared_preload_libraries 预加载 pg_search。")
