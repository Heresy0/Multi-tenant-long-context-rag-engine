import hashlib
import mimetypes
from collections.abc import Sequence
from math import isfinite
from pathlib import Path
from uuid import UUID

from langchain_core.embeddings import Embeddings
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import Settings
from .db.models import (
    EMBEDDING_DIMENSION,
    DocumentChunk,
    KnowledgeDocument,
)
from .rag import (
    CHUNKING_VERSION,
    calculate_file_hash,
    create_embeddings,
    split_file,
)
from .security.retrieval_scope import RetrievalScope


class PgVectorIndexingService:
    """将文件切分并以事务方式写入pgvector。"""

    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        embeddings: Embeddings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._embeddings = (
            embeddings or create_embeddings(settings)
        )

    def index_file(
        self,
        *,
        file_path: str | Path,
        scope: RetrievalScope,
        created_by_user_id: UUID,
    ) -> int:
        path = Path(file_path).resolve()
        file_hash = calculate_file_hash(path)
        source_id = self._calculate_source_id(path)

        try:
            current = self._is_current(
                scope=scope,
                source_id=source_id,
                file_hash=file_hash,
            )
        finally:
            # 结束只读事务，避免调用外部模型期间占用事务。
            self._session.rollback()

        if current:
            return 0

        chunks = split_file(path)

        if not chunks:
            raise ValueError(
                f"文档没有可索引的内容：{path}"
            )

        embeddings = self._embeddings.embed_documents([
            chunk.page_content
            for chunk in chunks
        ])

        if len(embeddings) != len(chunks):
            raise RuntimeError(
                "嵌入结果数量与文档分块数量不一致"
            )

        validated_embeddings = [
            self._validate_embedding(embedding)
            for embedding in embeddings
        ]

        try:
            document = self._find_document(
                scope=scope,
                source_id=source_id,
            )

            if document is None:
                document = KnowledgeDocument(
                    tenant_id=scope.tenant_id,
                    knowledge_base_id=(
                        scope.knowledge_base_id
                    ),
                    created_by_user_id=(
                        created_by_user_id
                    ),
                    source_id=source_id,
                    file_name=path.name,
                    storage_uri=path.as_uri(),
                    mime_type=(
                        mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream"
                    ),
                    content_hash=file_hash,
                    status="pending",
                    version=1,
                    metadata_json={},
                )
                self._session.add(document)
                self._session.flush()

            else:
                document.created_by_user_id = (
                    created_by_user_id
                )
                document.file_name = path.name
                document.storage_uri = path.as_uri()
                document.mime_type = (
                    mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream"
                )
                document.content_hash = file_hash
                document.status = "pending"
                document.version += 1
                document.last_error = None
                self._session.flush()

            self._session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.tenant_id
                    == scope.tenant_id,
                    DocumentChunk.knowledge_base_id
                    == scope.knowledge_base_id,
                    DocumentChunk.document_id
                    == document.id,
                )
            )

            new_chunks: list[DocumentChunk] = []

            for index, (
                chunk,
                embedding,
            ) in enumerate(
                zip(chunks, validated_embeddings)
            ):
                content_hash = hashlib.sha256(
                    chunk.page_content.encode("utf-8")
                ).hexdigest()

                parent_key = chunk.metadata.get(
                    "parent_key",
                    index,
                )
                parent_id = hashlib.sha256(
                    (
                        f"{scope.tenant_id}:"
                        f"{scope.knowledge_base_id}:"
                        f"{source_id}:"
                        f"{file_hash}:"
                        f"{parent_key}"
                    ).encode("utf-8")
                ).hexdigest()

                chunk_id = hashlib.sha256(
                    (
                        f"{scope.tenant_id}:"
                        f"{scope.knowledge_base_id}:"
                        f"{source_id}:"
                        f"{file_hash}:"
                        f"{CHUNKING_VERSION}:"
                        f"{index}:"
                        f"{content_hash}"
                    ).encode("utf-8")
                ).hexdigest()

                new_chunks.append(
                    DocumentChunk(
                        id=chunk_id,
                        tenant_id=scope.tenant_id,
                        knowledge_base_id=(
                            scope.knowledge_base_id
                        ),
                        document_id=document.id,
                        chunk_index=index,
                        content=chunk.page_content,
                        content_hash=content_hash,
                        parent_id=parent_id,
                        chunking_version=(
                            CHUNKING_VERSION
                        ),
                        embedding_model=(
                            self._settings.embedding_model
                        ),
                        embedding=embedding,
                        metadata_json=dict(
                            chunk.metadata
                        ),
                    )
                )

            self._session.add_all(new_chunks)

            document.status = "ready"
            document.metadata_json = {
                **dict(document.metadata_json),
                "chunk_count": len(new_chunks),
                "chunking_version": CHUNKING_VERSION,
                "embedding_model": (
                    self._settings.embedding_model
                ),
            }

            self._session.commit()

        except Exception:
            self._session.rollback()
            raise

        return len(new_chunks)

    def _find_document(
        self,
        *,
        scope: RetrievalScope,
        source_id: str,
    ) -> KnowledgeDocument | None:
        return self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.source_id
                == source_id,
            )
        )

    def _is_current(
        self,
        *,
        scope: RetrievalScope,
        source_id: str,
        file_hash: str,
    ) -> bool:
        document = self._find_document(
            scope=scope,
            source_id=source_id,
        )

        if (
            document is None
            or document.status != "ready"
            or document.content_hash != file_hash
        ):
            return False

        metadata = dict(document.metadata_json)

        if (
            metadata.get("chunking_version")
            != CHUNKING_VERSION
            or metadata.get("embedding_model")
            != self._settings.embedding_model
        ):
            return False

        expected_count = metadata.get("chunk_count")

        if (
            not isinstance(expected_count, int)
            or isinstance(expected_count, bool)
            or expected_count < 1
        ):
            return False

        actual_count = self._session.scalar(
            select(func.count(DocumentChunk.id)).where(
                DocumentChunk.tenant_id
                == scope.tenant_id,
                DocumentChunk.knowledge_base_id
                == scope.knowledge_base_id,
                DocumentChunk.document_id
                == document.id,
            )
        )

        return actual_count == expected_count

    @staticmethod
    def _calculate_source_id(path: Path) -> str:
        return hashlib.sha256(
            str(path).lower().encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _validate_embedding(
        values: Sequence[float],
    ) -> list[float]:
        if len(values) != EMBEDDING_DIMENSION:
            raise ValueError(
                "文档向量维度必须为"
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
                    "文档向量只能包含有限数值"
                )

            result.append(float(value))

        return result