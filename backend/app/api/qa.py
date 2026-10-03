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

from ..knowledge.answer_service import AnswerService
from ..knowledge.conversations import ConversationBusy, ConversationNotFound
from ..audit_service import AuditService
from ..db.dependencies import get_database_session
from ..schemas import (
    KnowledgeQuestionRequest,
    KnowledgeQuestionResponse,
)
from ..monitoring.observability import event_message
from ..monitoring.metrics import record_qa_request
from ..security.authorization import (
    AuthorizationDenied,
    AuthorizationService,
)
from ..security.dependencies import (
    get_current_principal,
)
from ..security.principal import Principal

from ..governance.tenant_rate_limiter import (
    RateLimitDecision,
    RateLimiterUnavailable,
    TenantRateLimiter,
    TenantRateLimitExceeded,
)
from ..governance.tenant_concurrency_limiter import (
    ConcurrencyDecision,
    ConcurrencyLease,
    ConcurrencyLimiterUnavailable,
    TenantConcurrencyExceeded,
    TenantConcurrencyLimiter,
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


def get_audit_service(
    session: Session = Depends(get_database_session),
) -> AuditService:
    return AuditService(session)


def get_tenant_rate_limiter(
    request: Request,
) -> TenantRateLimiter:
    return request.app.state.tenant_rate_limiter


def rate_limit_headers(
    decision: RateLimitDecision,
) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(
            decision.remaining
        ),
        "X-RateLimit-Reset-After": str(
            decision.reset_after_seconds
        ),
    }


def get_tenant_concurrency_limiter(
    request: Request,
) -> TenantConcurrencyLimiter:
    return (
        request.app.state
        .tenant_concurrency_limiter
    )


def concurrency_headers(
    decision: ConcurrencyDecision,
) -> dict[str, str]:
    return {
        "X-Concurrency-Limit": str(
            decision.limit
        ),
        "X-Concurrency-Remaining": str(
            decision.remaining
        ),
    }


@router.post(
    "",
    response_model=KnowledgeQuestionResponse,
    response_model_exclude_unset=True,
)
def answer_question(
    payload: KnowledgeQuestionRequest,
    request: Request,
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
    rate_limiter: TenantRateLimiter = Depends(
        get_tenant_rate_limiter
    ),
    concurrency_limiter: TenantConcurrencyLimiter = Depends(
        get_tenant_concurrency_limiter
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

    concurrency_lease: (
        ConcurrencyLease | None
    ) = None

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

        decision = rate_limiter.consume(
            tenant_id=principal.tenant_id,
            resource="qa",
            limit=(
                request.app.state.settings
                .qa_rate_limit_requests
            ),
            window_seconds=(
                request.app.state.settings
                .qa_rate_limit_window_seconds
            ),
        )

        if not decision.allowed:
            raise TenantRateLimitExceeded(
                decision
            )

        for header, value in (
            rate_limit_headers(decision).items()
        ):
            response.headers[header] = value

        concurrency_decision = (
            concurrency_limiter.acquire(
                tenant_id=principal.tenant_id,
                resource="qa",
                limit=(
                    request.app.state.settings
                    .qa_max_concurrent_requests_per_tenant
                ),
                lease_seconds=(
                    request.app.state.settings
                    .qa_concurrency_lease_seconds
                ),
            )
        )

        if not concurrency_decision.allowed:
            raise TenantConcurrencyExceeded(
                concurrency_decision
            )

        concurrency_lease = (
            concurrency_decision.lease
        )

        if concurrency_lease is None:
            raise ConcurrencyLimiterUnavailable(
                "并发限制服务未返回有效租约。"
            )

        for header, value in (
            concurrency_headers(
                concurrency_decision
            ).items()
        ):
            response.headers[header] = value

        def record_answer_success(result):
            commit_audit(outcome="success", details={
                "answerable": result.answerable, "citation_count": len(result.citations),
            })

        conversation_fields = {}
        if payload.conversation_id is not None:
            result, context_ms = request.app.state.conversation_answer_service.answer(
                payload.question, conversation_id=payload.conversation_id,
                principal=principal, scope=scope, session=session, commit_success=record_answer_success,
            )
            conversation_fields = {
                "conversation_id": payload.conversation_id,
                "conversation_context_ms": context_ms,
            }
        else:
            result = answer_service.answer(
                payload.question,
                scope=scope,
                session=session,
            )
            record_answer_success(result)

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
            **result.model_dump(), **conversation_fields
        )

    except (ConversationNotFound, ConversationBusy) as exc:
        missing = isinstance(exc, ConversationNotFound)
        commit_audit(outcome="denied" if missing else "invalid")
        record_qa_request(
            outcome="denied" if missing else "invalid", answerable=None,
            duration_seconds=perf_counter() - started,
        )
        raise HTTPException(
            status_code=404 if missing else 409, detail=str(exc),
            headers={"X-Request-ID": request_id},
        ) from exc

    except TenantRateLimitExceeded as exc:
        decision = exc.decision

        commit_audit(
            outcome="denied",
            details={
                "reason": "rate_limited",
                "limit": decision.limit,
                "window_seconds": (
                    request.app.state.settings
                    .qa_rate_limit_window_seconds
                ),
            },
        )

        record_qa_request(
            outcome="rate_limited",
            answerable=None,
            duration_seconds=(
                perf_counter() - started
            ),
        )

        logger.warning(
            event_message(
                "qa.rate_limited",
                **common_fields,
                status_code=429,
                limit=decision.limit,
                retry_after_seconds=(
                    decision.reset_after_seconds
                ),
            )
        )

        headers = rate_limit_headers(decision)
        headers["Retry-After"] = str(
            decision.reset_after_seconds
        )
        headers["X-Request-ID"] = request_id

        raise HTTPException(
            status_code=(
                status.HTTP_429_TOO_MANY_REQUESTS
            ),
            detail=(
                "该租户的问答请求过于频繁，"
                "请稍后重试。"
            ),
            headers=headers,
        ) from exc

    except RateLimiterUnavailable as exc:
        commit_audit(
            outcome="failed",
            details={
                "reason": "rate_limiter_unavailable",
            },
        )

        record_qa_request(
            outcome="rate_limiter_unavailable",
            answerable=None,
            duration_seconds=(
                perf_counter() - started
            ),
        )

        logger.error(
            event_message(
                "qa.rate_limiter_unavailable",
                **common_fields,
                status_code=503,
            )
        )

        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail="请求治理服务暂时不可用。",
            headers={
                "X-Request-ID": request_id,
            },
        ) from exc

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

    except TenantConcurrencyExceeded as exc:
        decision = exc.decision

        commit_audit(
            outcome="denied",
            details={
                "reason": "concurrency_limited",
                "limit": decision.limit,
                "active": decision.active,
            },
        )

        record_qa_request(
            outcome="concurrency_limited",
            answerable=None,
            duration_seconds=(
                perf_counter() - started
            ),
        )

        logger.warning(
            event_message(
                "qa.concurrency_limited",
                **common_fields,
                status_code=429,
                limit=decision.limit,
                active=decision.active,
                retry_after_seconds=(
                    decision.retry_after_seconds
                ),
            )
        )

        headers = concurrency_headers(decision)
        headers["Retry-After"] = str(
            max(
                decision.retry_after_seconds,
                1,
            )
        )
        headers["X-Request-ID"] = request_id

        raise HTTPException(
            status_code=(
                status.HTTP_429_TOO_MANY_REQUESTS
            ),
            detail=(
                "该租户当前正在处理的问答请求"
                "过多，请稍后重试。"
            ),
            headers=headers,
        ) from exc

    except ConcurrencyLimiterUnavailable as exc:
        commit_audit(
            outcome="failed",
            details={
                "reason": (
                    "concurrency_limiter_unavailable"
                ),
            },
        )

        record_qa_request(
            outcome=(
                "concurrency_limiter_unavailable"
            ),
            answerable=None,
            duration_seconds=(
                perf_counter() - started
            ),
        )

        logger.error(
            event_message(
                "qa.concurrency_limiter_unavailable",
                **common_fields,
                status_code=503,
            )
        )

        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail="并发治理服务暂时不可用。",
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

    finally:
        if concurrency_lease is not None:
            try:
                concurrency_limiter.release(
                    concurrency_lease
                )
            except ConcurrencyLimiterUnavailable:
                logger.exception(
                    event_message(
                        "qa.concurrency_release_failed",
                        **common_fields,
                    )
                )
