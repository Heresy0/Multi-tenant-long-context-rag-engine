from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
    File,
    Request,
    Response,
    UploadFile,
    )
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid import UUID

from ..db.dependencies import get_database_session
from ..db.models import KnowledgeBase, KnowledgeDocument
from ..schemas import (
    KnowledgeBaseListResponse,
    KnowledgeBaseSummary,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentSummary,
    KnowledgeDocumentUploadResponse
)
from ..security.authorization import (
    AuthorizationService,
    AuthorizationDenied,
)
from ..security.dependencies import (
    get_current_principal,
)
from ..security.principal import Principal

from ..document_upload_service import (
    DocumentUploadService,
    InvalidUpload,
    UploadTooLarge,
)
from ..pgvector_indexing_service import (
    PgVectorIndexingService,
)
from ..security.retrieval_scope import RetrievalScope

from ..document_deletion_service import (
    DocumentDeletionService,
    DocumentNotFoundError,
)


router = APIRouter(
    prefix="/api/knowledge-bases",
    tags=["knowledge-bases"],
)


@router.get(
    "",
    response_model=KnowledgeBaseListResponse,
)
def list_accessible_knowledge_bases(
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> KnowledgeBaseListResponse:
    """列出当前用户有权查询的知识库。"""
    authorization = AuthorizationService(session)

    readable_ids = (
        authorization.list_readable_knowledge_base_ids(
            principal=principal
        )
    )

    if not readable_ids:
        return KnowledgeBaseListResponse(items=[])

    knowledge_bases = session.scalars(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id
            == principal.tenant_id,
            KnowledgeBase.id.in_(readable_ids),
            KnowledgeBase.status == "active",
        )
        .order_by(KnowledgeBase.name)
    ).all()

    items: list[KnowledgeBaseSummary] = []

    for knowledge_base in knowledge_bases:
        permission = authorization.get_permission(
            principal=principal,
            knowledge_base_id=knowledge_base.id,
        )

        if permission is None:
            continue

        items.append(
            KnowledgeBaseSummary(
                id=knowledge_base.id,
                name=knowledge_base.name,
                visibility=knowledge_base.visibility,
                permission=permission,
            )
        )

    return KnowledgeBaseListResponse(items=items)


def _document_chunk_count(
    document: KnowledgeDocument,
) -> int:
    value = document.metadata_json.get(
        "chunk_count",
        0,
    )

    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value

    return 0


@router.get(
    "/{knowledge_base_id}/documents",
    response_model=KnowledgeDocumentListResponse,
)
def list_knowledge_base_documents(
    knowledge_base_id: UUID,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> KnowledgeDocumentListResponse:
    """列出当前用户有权查看的知识库文档。"""
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="viewer",
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    documents = session.scalars(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id
            == principal.tenant_id,
            KnowledgeDocument.knowledge_base_id
            == knowledge_base_id,
        )
        .order_by(
            KnowledgeDocument.updated_at.desc(),
            KnowledgeDocument.id,
        )
    ).all()

    return KnowledgeDocumentListResponse(
        items=[
            KnowledgeDocumentSummary(
                id=document.id,
                file_name=document.file_name,
                mime_type=document.mime_type,
                status=document.status,
                version=document.version,
                chunk_count=_document_chunk_count(
                    document
                ),
                created_at=document.created_at,
                updated_at=document.updated_at,
            )
            for document in documents
        ]
    )


@router.post(
    "/{knowledge_base_id}/documents",
    response_model=KnowledgeDocumentUploadResponse,
)
def upload_knowledge_base_document(
    knowledge_base_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> KnowledgeDocumentUploadResponse:
    """上传文件并写入指定知识库。"""
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="editor",
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    scope = RetrievalScope(
        tenant_id=principal.tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    settings = request.app.state.settings

    indexing_service = PgVectorIndexingService(
        session=session,
        settings=settings,
    )

    upload_service = DocumentUploadService(
        session=session,
        indexing_service=indexing_service,
        storage_dir=settings.document_storage_dir,
        max_upload_bytes=settings.max_upload_bytes,
    )

    try:
        result = upload_service.upload(
            file_name=file.filename or "",
            source=file.file,
            scope=scope,
            created_by_user_id=principal.user_id,
        )

    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc

    except InvalidUpload as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="文档入库失败，请稍后重试。",
        ) from exc

    document = result.document

    return KnowledgeDocumentUploadResponse(
        document=KnowledgeDocumentSummary(
            id=document.id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            status=document.status,
            version=document.version,
            chunk_count=_document_chunk_count(
                document
            ),
            created_at=document.created_at,
            updated_at=document.updated_at,
        ),
        indexed_chunk_count=(
            result.indexed_chunk_count
        ),
        skipped=result.skipped,
    )


@router.delete(
    "/{knowledge_base_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_knowledge_base_document(
    knowledge_base_id: UUID,
    document_id: UUID,
    request: Request,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> Response:
    """删除指定知识库中的文档。"""
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="editor",
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    scope = RetrievalScope(
        tenant_id=principal.tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    deletion_service = DocumentDeletionService(
        session=session,
        storage_dir=(
            request.app.state.settings
            .document_storage_dir
        ),
    )

    try:
        deletion_service.delete(
            scope=scope,
            document_id=document_id,
        )

    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail="文档删除失败，请稍后重试。",
        ) from exc

    return Response(
        status_code=status.HTTP_204_NO_CONTENT
    )