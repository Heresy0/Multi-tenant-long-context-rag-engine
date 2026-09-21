import json
import logging

from contextlib import ExitStack, asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, APIRouter, Depends
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse

from ..agent import EnterpriseAgent
from ..config import PROJECT_ROOT, Settings
from ..schemas import ChatRequest, ChatResponse
from ..chat_history import ChatHistoryRepository


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/chat",
    tags=["chat"],
)

def get_enterprise_agent(request: Request,) -> EnterpriseAgent:
    """从 FastAPI 应用状态中取得共享agent。"""
    return request.app.state.enterprise_agent

def get_chat_history(
        request: Request,
) -> ChatHistoryRepository:
    """从 FastAPI 应用状态中取得聊天历史仓库。"""
    return request.app.state.chat_history


def create_sse_event(event: str,data: dict) -> str:
    """将数据转换成 SSE 事件格式。"""
    json_data = json.dumps(data, ensure_ascii=False,)

    return(
        f"event: {event}\n"
        f"data: {json_data}\n\n"
    )


def extract_text(content) -> str:
    """兼容字符串和内容块形式的模型输出。"""
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts = []

    for block in content:
        if isinstance(block, str):
            parts.append(block)

        elif isinstance(block, dict):
            text = block.get("text")

            if text:
                parts.append(text)

    return "".join(parts)





@router.post("")
def chat(
    payload: ChatRequest,
    enterprise_agent:EnterpriseAgent = Depends(get_enterprise_agent),
    history_repository: ChatHistoryRepository = Depends(get_chat_history),
):
    """流式企业知识库聊天接口。"""
    message = payload.message.strip()

    if not message:
        raise HTTPException(status_code=422,detail="消息不能为空。")

    conversation_id = (payload.conversation_id or str(uuid4()))

    def event_stream():
        answer_parts: list[str] = []

        #首先告诉前端本次会话 ID
        yield create_sse_event(
            event = "metadata",
            data = {"conversation_id": conversation_id,},
        )


        try:
            # 保存用户消息
            history_repository.add_message(
                conversation_id=conversation_id,
                role="user",
                content=message,
            )

            stream =  enterprise_agent.stream(
                message = message,
                thread_id = conversation_id,
            )

            for message_chunk,_metadata in stream:
                if not isinstance(
                    message_chunk,
                    AIMessageChunk,
                ):
                    continue

                text = extract_text(message_chunk.content)

                if not text:
                    continue

                # 累加完整回答
                answer_parts.append(text)

                # 将当前文本块发送给前端
                yield create_sse_event(
                    event="token",
                    data={"content": text,},
                )

            #拼接chunk
            answer = "".join(answer_parts)

            # 流式生成完成后保存助手完整回答
            if answer:history_repository.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=answer,
            )

            yield create_sse_event(
                event="done",
                data={"conversation_id": conversation_id,},
            )

        except Exception:
            logger.exception(
                "Agent流式调用失败,conversation_id=%s",
                conversation_id,
            )

            #SSE 已经开始响应，不能再返回 HTTPException
            yield create_sse_event(
                event="error",
                data={
                    "message":"Agent服务暂时不可用。",
                    "conversation_id": conversation_id,
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":"no-cache",
            "Connection":"keep-alive",
            #防止Nginx缓冲流式响应
            "X-Accel-Buffering":"no",
            "X-Conversation-ID":conversation_id,
        },
    )



@router.get("")
def get_conversations(
    history_repository: ChatHistoryRepository = Depends(
        get_chat_history
    ),
):
    """查询全部历史会话。"""
    return{
        "conversations":(history_repository.list_conversations()),
    }

@router.get("/{conversation_id}/messages")
def get_chat_messages(
    conversation_id: str,
    history_repository: ChatHistoryRepository = Depends(
        get_chat_history
    ),
):
    """查询指定会话的完整消息记录"""
    return{
        "conversation_id": conversation_id,
        "messages": history_repository.list_messages(conversation_id),
    }