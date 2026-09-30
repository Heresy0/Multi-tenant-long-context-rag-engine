import logging
from time import perf_counter
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy.orm import Session

from ..answer_service import AnswerService
from ..audit_service import AuditService
from ..db.dependencies import get_database_session
from ..schemas import (
    KnowledgeQuestionRequest,
    KnowledgeQuestionResponse,
)
from ..observability import event_message
from ..metrics import record_qa_request
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


def get_audit_service(
    session: Session = Depends(get_database_session),
) -> AuditService:
    return AuditService(session)


@router.post(
    "",
    response_model=KnowledgeQuestionResponse,
)
def answer_question(
    payload: KnowledgeQuestionRequest,
    response: Response,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
    answer_service: AnswerService = Depends(
        get_answer_service
    ),
    audit_service: AuditService = Depends(
        get_audit_service
    ),
) -> KnowledgeQuestionResponse:
    """在授权知识库范围内执行问答。"""
    request_id = uuid4().hex
    started = perf_counter()
    response.headers["X-Request-ID"] = request_id

    common_fields = {
        "request_id": request_id,
        "user_id": str(principal.user_id),
        "tenant_id": str(principal.tenant_id),
        "knowledge_base_id": str(
            payload.knowledge_base_id
        ),
    }

    def commit_audit(
        *,
        outcome: str,
        details: dict[str, object] | None = None,
    ) -> None:
        audit_service.record(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            knowledge_base_id=(
                payload.knowledge_base_id
            ),
            action="qa.queried",
            resource_type="knowledge_base",
            resource_id=payload.knowledge_base_id,
            outcome=outcome,
            request_id=request_id,
            details=details,
        )

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
        commit_audit(
            outcome="success",
            details={
                "answerable": result.answerable,
                "citation_count": len(
                    result.citations
                ),
            },
        )

        timing_fields = (
            result.timings.model_dump()
            if result.timings is not None
            else {}
        )

        logger.info(
            event_message(
                "qa.completed",
                **common_fields,
                status_code=200,
                answerable=result.answerable,
                citation_count=len(result.citations),
                **timing_fields,
            )
        )
        record_qa_request(
            outcome="completed",
            answerable=result.answerable,
            duration_seconds=(
                perf_counter() - started
            ),
            timings=result.timings,
        )

        return KnowledgeQuestionResponse(
            **result.model_dump()
        )

    except AuthorizationDenied as exc:
        commit_audit(outcome="denied")
        record_qa_request(
            outcome="denied",
            answerable=None,
            duration_seconds=(
                perf_counter() - started
            ),
        )
        logger.warning(
            event_message(
                "qa.denied",
                **common_fields,
                status_code=403,
            )
        )

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
            headers={
                "X-Request-ID": request_id,
            },
        ) from exc

    except ValueError as exc:
        commit_audit(outcome="invalid")
        record_qa_request(
            outcome="invalid",
            answerable=None,
            duration_seconds=(
                perf_counter() - started
            ),
        )
        logger.warning(
            event_message(
                "qa.invalid",
                **common_fields,
                status_code=422,
            )
        )

        raise HTTPException(
            status_code=422,
            detail=str(exc),
            headers={
                "X-Request-ID": request_id,
            },
        ) from exc

    except Exception as exc:
        try:
            commit_audit(
                outcome="failed",
                details={
                    "error_type": type(exc).__name__,
                },
            )
        except Exception:
            logger.exception(
                "问答失败审计事件写入失败，request_id=%s",
                request_id,
            )
        record_qa_request(
            outcome="failed",
            answerable=None,
            duration_seconds=(
                perf_counter() - started
            ),
        )
        logger.exception(
            event_message(
                "qa.failed",
                **common_fields,
                status_code=503,
                error_type=type(exc).__name__,
            )
        )

        raise HTTPException(
            status_code=503,
            detail="知识库问答服务暂时不可用。",
            headers={
                "X-Request-ID": request_id,
            },
        ) from exc
