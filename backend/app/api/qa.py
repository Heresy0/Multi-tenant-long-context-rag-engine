import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from sqlalchemy.orm import Session

from ..answer_service import AnswerService
from ..db.dependencies import get_database_session
from ..schemas import (
    KnowledgeQuestionRequest,
    KnowledgeQuestionResponse,
)
from ..security.authorization import (
    AuthorizationDenied,
    AuthorizationService,
)
from ..security.dependencies import (
    get_current_principal,
)
from ..security.principal import Principal


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/qa",
    tags=["qa"],
)


def get_answer_service(
    request: Request,
) -> AnswerService:
    """从应用状态中取得共享回答服务。"""
    return request.app.state.answer_service


@router.post(
    "",
    response_model=KnowledgeQuestionResponse,
)
def answer_question(
    payload: KnowledgeQuestionRequest,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
    answer_service: AnswerService = Depends(
        get_answer_service
    ),
) -> KnowledgeQuestionResponse:
    """在授权知识库范围内执行问答。"""
    try:
        authorization = AuthorizationService(session)

        scope = (
            authorization.require_retrieval_scope(
                principal=principal,
                knowledge_base_id=(
                    payload.knowledge_base_id
                ),
            )
        )

        result = answer_service.answer(
            payload.question,
            scope=scope,
            session=session,
        )

        return KnowledgeQuestionResponse(
            **result.model_dump()
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.exception(
            "知识库问答服务调用失败"
        )

        raise HTTPException(
            status_code=503,
            detail="知识库问答服务暂时不可用。",
        ) from exc