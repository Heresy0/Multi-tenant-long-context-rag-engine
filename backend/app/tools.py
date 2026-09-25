from langchain_core.tools import tool
from .retrieval_service import RetrievalService
from .context_builder import ContextBuilder


def create_search_knowledge_base_tool(
    retrieval_service: RetrievalService,
):
    context_builder = ContextBuilder(
        max_characters=12000,
        max_chunks_per_source=4,
    )

    @tool
    def search_knowledge_base(query: str) -> str:
        """检索企业内部知识库。"""

        documents = retrieval_service.search(query)
        context = context_builder.build(documents)

        if not context.items:
            return "未找到能够回答问题的知识库资料。"

        return context.text

    return search_knowledge_base

