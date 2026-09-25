import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..answer_service import AnswerService
from ..schemas import (
    KnowledgeQuestionRequest,
    KnowledgeQuestionResponse,
)


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
    answer_service: AnswerService = Depends(
        get_answer_service
    ),
) -> KnowledgeQuestionResponse:
    """执行经过引用校验的知识库问答。"""
    try:
        result = answer_service.answer(
            payload.question
        )

        return KnowledgeQuestionResponse(
            **result.model_dump()
        )

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