from .agent import EnterpriseAgent
from .config import Settings,PROJECT_ROOT
from langgraph.checkpoint.sqlite import SqliteSaver
from contextlib import ExitStack, asynccontextmanager
from fastapi import FastAPI
from .api.chat import router as chat_router
from .chat_history import ChatHistoryRepository


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时创建agent,关闭时释放SQLite连接。"""
    database_path = (PROJECT_ROOT / "agent_memory.sqlite")
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with ExitStack() as stack:
        checkpointer = stack.enter_context(SqliteSaver.from_conn_string(str(database_path)))
        checkpointer.setup()

        app.state.enterprise_agent = EnterpriseAgent(
            settings=Settings(),
            checkpointer=checkpointer,
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

@app.get("/health",tags=["系统"])
def health_check():
    return{"status":"ok"}
