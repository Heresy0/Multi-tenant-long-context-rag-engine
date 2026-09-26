from .agent import EnterpriseAgent
from .config import Settings,PROJECT_ROOT
from langgraph.checkpoint.sqlite import SqliteSaver
from contextlib import ExitStack, asynccontextmanager
from fastapi import FastAPI
from .api.chat import router as chat_router
from .chat_history import ChatHistoryRepository
from .retrieval_service import RetrievalService
from .answer_service import AnswerService
from .api.qa import router as qa_router
from .db.session import (
    create_database_engine,
    create_session_factory,
    verify_database_connection,
)
from .api.knowledge_bases import (
    router as knowledge_base_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化应用服务，并在关闭时释放数据库连接。"""
    database_path = (PROJECT_ROOT / "agent_memory.sqlite")
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with ExitStack() as stack:
        checkpointer = stack.enter_context(SqliteSaver.from_conn_string(str(database_path)))
        checkpointer.setup()

        settings = Settings()

        database_engine = create_database_engine(
            settings.database_url
        )

        #即使后续初始化失败，也释放连接池
        stack.callback(database_engine.dispose)

        verify_database_connection(database_engine)

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

        app.state.retrieval_service = retrieval_service

        app.state.enterprise_agent = EnterpriseAgent(
            settings=settings,
            checkpointer=checkpointer,
            retrieval_service=retrieval_service,
        )

        history_repository = ChatHistoryRepository(
            PROJECT_ROOT/"chat_history.sqlite"
        )
        history_repository.setup()

        app.state.chat_history = history_repository

        yield


app = FastAPI(
    title="企业内部智能助手",
    version="1.0.0",
    lifespan=lifespan
    )

app.include_router(chat_router)
app.include_router(qa_router)
app.include_router(knowledge_base_router)

@app.get("/health",tags=["系统"])
def health_check():
    return{"status":"ok"}
