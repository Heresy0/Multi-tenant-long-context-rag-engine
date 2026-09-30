import hashlib
import mimetypes
from collections.abc import Sequence
from math import isfinite
from pathlib import Path
from uuid import UUID

from langchain_core.embeddings import Embeddings
from langchain_core.documents import Document
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
    calculate_source_id,
    create_embeddings,
    split_file,
)
from .security.retrieval_scope import RetrievalScope


class DocumentCandidateNotFound(LookupError):
    """候选文件对应的文档不存在。"""


class StaleDocumentCandidate(RuntimeError):
    """候选文件的目标版本已经失效。"""


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
        source_id = calculate_source_id(path)

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

        chunks, validated_embeddings = (
            self._prepare_chunks(
                path=path,
                document_name=path.stem,
                source=str(path),
            )
        )

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

            new_chunks = self._build_chunk_records(
                chunks=chunks,
                embeddings=validated_embeddings,
                scope=scope,
                document_id=document.id,
                source_id=source_id,
                file_hash=file_hash,
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

    def index_document_candidate(
        self,
        *,
        file_path: str | Path,
        scope: RetrievalScope,
        document_id: UUID,
        created_by_user_id: UUID,
        target_version: int,
        candidate_file_name: str,
        candidate_mime_type: str,
        candidate_content_hash: str,
        final_storage_uri: str,
    ) -> int:
        """把暂存候选文件写入指定文档和目标版本。"""
        if target_version < 1:
            raise ValueError(
                "target_version 必须大于等于 1"
            )

        candidate_values = {
            "candidate_file_name": candidate_file_name,
            "candidate_mime_type": candidate_mime_type,
            "candidate_content_hash": (
                candidate_content_hash
            ),
            "final_storage_uri": final_storage_uri,
        }

        if any(
            not value.strip()
            for value in candidate_values.values()
        ):
            raise ValueError(
                "候选文件信息不能为空"
            )

        path = Path(file_path).resolve()
        actual_hash = calculate_file_hash(path)

        if actual_hash != candidate_content_hash:
            raise ValueError(
                "暂存文件内容哈希不匹配"
            )

        try:
            document = self._find_document_by_id(
                scope=scope,
                document_id=document_id,
            )

            if document is None:
                raise DocumentCandidateNotFound(
                    "待索引文档不存在"
                )

            current = self._candidate_is_current(
                document=document,
                target_version=target_version,
                candidate_file_name=(
                    candidate_file_name
                ),
                candidate_mime_type=(
                    candidate_mime_type
                ),
                candidate_content_hash=(
                    candidate_content_hash
                ),
                final_storage_uri=final_storage_uri,
            )

            if not current:
                self._validate_candidate_version(
                    document=document,
                    target_version=target_version,
                    candidate_file_name=(
                        candidate_file_name
                    ),
                    candidate_mime_type=(
                        candidate_mime_type
                    ),
                    candidate_content_hash=(
                        candidate_content_hash
                    ),
                    final_storage_uri=(
                        final_storage_uri
                    ),
                )

        finally:
            # 切分和嵌入期间不持有数据库事务。
            self._session.rollback()

        if current:
            return 0

        chunks, validated_embeddings = (
            self._prepare_chunks(
                path=path,
                document_name=(
                    Path(candidate_file_name).stem
                ),
                source=final_storage_uri,
            )
        )

        try:
            document = self._session.scalar(
                select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.tenant_id
                    == scope.tenant_id,
                    KnowledgeDocument.knowledge_base_id
                    == scope.knowledge_base_id,
                    KnowledgeDocument.id
                    == document_id,
                )
                .with_for_update()
            )

            if document is None:
                raise DocumentCandidateNotFound(
                    "待索引文档不存在"
                )

            if self._candidate_is_current(
                document=document,
                target_version=target_version,
                candidate_file_name=(
                    candidate_file_name
                ),
                candidate_mime_type=(
                    candidate_mime_type
                ),
                candidate_content_hash=(
                    candidate_content_hash
                ),
                final_storage_uri=final_storage_uri,
            ):
                self._session.rollback()
                return 0

            self._validate_candidate_version(
                document=document,
                target_version=target_version,
                candidate_file_name=(
                    candidate_file_name
                ),
                candidate_mime_type=(
                    candidate_mime_type
                ),
                candidate_content_hash=(
                    candidate_content_hash
                ),
                final_storage_uri=final_storage_uri,
            )

            new_chunks = self._build_chunk_records(
                chunks=chunks,
                embeddings=validated_embeddings,
                scope=scope,
                document_id=document.id,
                source_id=document.source_id,
                file_hash=candidate_content_hash,
            )

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
            self._session.add_all(new_chunks)

            document.created_by_user_id = (
                created_by_user_id
            )
            document.file_name = candidate_file_name
            document.storage_uri = final_storage_uri
            document.mime_type = candidate_mime_type
            document.content_hash = (
                candidate_content_hash
            )
            document.status = "ready"
            document.version = target_version
            document.last_error = None
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

    def _find_document_by_id(
        self,
        *,
        scope: RetrievalScope,
        document_id: UUID,
    ) -> KnowledgeDocument | None:
        return self._session.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id
                == scope.tenant_id,
                KnowledgeDocument.knowledge_base_id
                == scope.knowledge_base_id,
                KnowledgeDocument.id == document_id,
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

    def _candidate_is_current(
        self,
        *,
        document: KnowledgeDocument,
        target_version: int,
        candidate_file_name: str,
        candidate_mime_type: str,
        candidate_content_hash: str,
        final_storage_uri: str,
    ) -> bool:
        if (
            document.status != "ready"
            or document.version != target_version
            or not self._candidate_identity_matches(
                document=document,
                candidate_file_name=(
                    candidate_file_name
                ),
                candidate_mime_type=(
                    candidate_mime_type
                ),
                candidate_content_hash=(
                    candidate_content_hash
                ),
                final_storage_uri=final_storage_uri,
            )
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
                == document.tenant_id,
                DocumentChunk.knowledge_base_id
                == document.knowledge_base_id,
                DocumentChunk.document_id
                == document.id,
            )
        )

        return actual_count == expected_count

    def _validate_candidate_version(
        self,
        *,
        document: KnowledgeDocument,
        target_version: int,
        candidate_file_name: str,
        candidate_mime_type: str,
        candidate_content_hash: str,
        final_storage_uri: str,
    ) -> None:
        if document.version == target_version:
            if self._candidate_identity_matches(
                document=document,
                candidate_file_name=(
                    candidate_file_name
                ),
                candidate_mime_type=(
                    candidate_mime_type
                ),
                candidate_content_hash=(
                    candidate_content_hash
                ),
                final_storage_uri=final_storage_uri,
            ):
                return

            raise StaleDocumentCandidate(
                "目标版本已经被其他候选文件占用"
            )

        if document.version != target_version - 1:
            raise StaleDocumentCandidate(
                "索引任务版本已经过期"
            )

    @staticmethod
    def _candidate_identity_matches(
        *,
        document: KnowledgeDocument,
        candidate_file_name: str,
        candidate_mime_type: str,
        candidate_content_hash: str,
        final_storage_uri: str,
    ) -> bool:
        return (
            document.file_name == candidate_file_name
            and document.mime_type
            == candidate_mime_type
            and document.content_hash
            == candidate_content_hash
            and document.storage_uri
            == final_storage_uri
        )

    def _prepare_chunks(
        self,
        *,
        path: Path,
        document_name: str,
        source: str,
    ) -> tuple[list[Document], list[list[float]]]:
        chunks = split_file(
            path,
            document_name=document_name,
        )

        if not chunks:
            raise ValueError(
                f"文档没有可索引的内容：{path}"
            )

        for chunk in chunks:
            chunk.metadata["source"] = source
            chunk.metadata["document_name"] = (
                document_name
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

        return chunks, validated_embeddings

    def _build_chunk_records(
        self,
        *,
        chunks: Sequence[Document],
        embeddings: Sequence[Sequence[float]],
        scope: RetrievalScope,
        document_id: UUID,
        source_id: str,
        file_hash: str,
    ) -> list[DocumentChunk]:
        records: list[DocumentChunk] = []

        for index, (
            chunk,
            embedding,
        ) in enumerate(zip(chunks, embeddings)):
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

            records.append(
                DocumentChunk(
                    id=chunk_id,
                    tenant_id=scope.tenant_id,
                    knowledge_base_id=(
                        scope.knowledge_base_id
                    ),
                    document_id=document_id,
                    chunk_index=index,
                    content=chunk.page_content,
                    content_hash=content_hash,
                    parent_id=parent_id,
                    chunking_version=CHUNKING_VERSION,
                    embedding_model=(
                        self._settings.embedding_model
                    ),
                    embedding=list(embedding),
                    metadata_json=dict(chunk.metadata),
                )
            )

        return records

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
