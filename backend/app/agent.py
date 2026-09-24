from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_openai import ChatOpenAI
from .prompts import SYSTEM_PROMPT,SUMMARY_PROMPT
from .tools import create_search_knowledge_base_tool
from .config import Settings
from .retrieval_service import RetrievalService


class EnterpriseAgent:
    """企业内部智能助手。"""
    def __init__(
            self,
            settings: Settings,
            checkpointer,
            retrieval_service: RetrievalService,
        ):


        llm = ChatOpenAI(
            model=settings.chat_model,
            base_url=settings.chat_base_url,
            api_key=settings.chat_api_key,
            temperature=0,
            timeout=60,
            max_retries=1,
        )
        self.vision_llm = llm

        summary_llm = ChatOpenAI(
            model=settings.summary_model,
            base_url=settings.summary_base_url,
            api_key=settings.summary_api_key,
            temperature=0,
            timeout=60,
            max_retries=1,
        )

        self.tools = [
            create_search_knowledge_base_tool(
                retrieval_service
            )
        ]

        self.agent = create_agent(
            model=llm,
            tools=self.tools,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=checkpointer,
            middleware=[SummarizationMiddleware(
                model=summary_llm,
                trigger=("tokens",5000),
                keep=("messages",10),
                summary_prompt=SUMMARY_PROMPT,
            ) ,  ],
        )


    def stream(self, message:str, thread_id:str,):
        config = {"configurable":{"thread_id":thread_id,}}

        return self.agent.stream({"messages":[
            {"role": "user", "content": message,}
        ]},
        config=config,
        stream_mode="messages",
        )