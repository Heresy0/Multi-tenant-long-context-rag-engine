from fastapi import APIRouter, Depends, HTTPException, status
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
)
from ..security.authorization import (
    AuthorizationService,
    AuthorizationDenied,
)
from ..security.dependencies import (
    get_current_principal,
)
from ..security.principal import Principal


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