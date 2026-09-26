from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.dependencies import get_database_session
from ..db.models import KnowledgeBase
from ..schemas import (
    KnowledgeBaseListResponse,
    KnowledgeBaseSummary,
)
from ..security.authorization import (
    AuthorizationService,
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