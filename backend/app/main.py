import logging

from contextlib import ExitStack, asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .knowledge.retrieval_service import RetrievalService
from .knowledge.answer_service import AnswerService
from .knowledge.conversation_service import ConversationAnswerService, ConversationResolver
from .api.conversations import router as conversations_router
from .api.qa import router as qa_router
from .api.system import router as system_router
from .api.frontend import router as frontend_router
from .db.session import (
    create_database_engine,
    create_session_factory,
    verify_database_connection,
)
from .api.knowledge_bases import (
    router as knowledge_base_router,
)
from .security.oidc import OidcTokenVerifier
from .monitoring.metrics import observe_http_request

from .governance.redis_client import (
    create_redis_client,
    verify_redis_connection,
)

from .governance.tenant_rate_limiter import TenantRateLimiter
from .governance.tenant_concurrency_limiter import (
    TenantConcurrencyLimiter,
)


def configure_application_logging(
    level_name: str,
) -> None:
    """配置 backend.app 下的应用日志。"""
    application_logger = logging.getLogger(
        "backend.app"
    )
    application_logger.setLevel(level_name)

    if not application_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(message)s")
        )
        application_logger.addHandler(handler)

    # 防止继续传递给根日志器而重复输出。
    application_logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化应用服务，并在关闭时释放数据库连接。"""
    with ExitStack() as stack:
        settings = Settings()
        app.state.settings = settings

        configure_application_logging(
            settings.log_level
        )

        app.state.oidc_token_verifier = OidcTokenVerifier(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
        )

        database_engine = create_database_engine(
            settings.database_url
        )

        #即使后续初始化失败，也释放连接池
        stack.callback(database_engine.dispose)

        verify_database_connection(database_engine)

        redis_client = create_redis_client(
            settings.redis_url,
            connect_timeout_seconds=(
                settings.redis_connect_timeout_seconds
            ),
            socket_timeout_seconds=(
                settings.redis_socket_timeout_seconds
            ),
        )

        stack.callback(redis_client.close)
        verify_redis_connection(redis_client)

        app.state.redis_client = redis_client
        app.state.tenant_rate_limiter = (
            TenantRateLimiter(redis_client)
        )
        app.state.tenant_concurrency_limiter = (
            TenantConcurrencyLimiter(redis_client)
        )
        app.state.database_engine = database_engine
        app.state.database_session_factory = (
            create_session_factory(database_engine)
        )

        retrieval_service = RetrievalService(
            settings=settings
        )

        answer_service = AnswerService(
            settings=settings,
            retrieval_service=retrieval_service,
        )

        app.state.answer_service = answer_service
        app.state.conversation_answer_service = ConversationAnswerService(
            answer_service, ConversationResolver(settings),
        )

        yield


app = FastAPI(
    title="企业内部智能助手",
    version="1.0.0",
    lifespan=lifespan
    )

app.middleware("http")(observe_http_request)

app.include_router(qa_router)
app.include_router(conversations_router)
app.include_router(knowledge_base_router)
app.include_router(system_router)
app.include_router(frontend_router)
