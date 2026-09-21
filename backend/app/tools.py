import requests
from langchain_core.tools import tool
from .rag import create_retriever


retriever = create_retriever()


@tool
def search_knowledge_base(query: str) -> str:
    """检索企业内部知识库。
    当用户询问企业内部知识库相关问题时，调用此工具进行检索。
    """

    documents = retriever.invoke(query)

    if not documents:
        return "未找到相关内容。"

    results = []

    for index,document in enumerate(documents, start=1):
        source = document.metadata.get("source","未知来源")
        page = document.metadata.get("page")

        source_text = f"来源：{source}"
        if page is not None:
            source_text += f",页码：{page + 1}"

        results.append(
            f"[资料{index}]\n"
            f"{source_text}\n"
            f"内容：{document.page_content}"
        )

    return "\n\n".join(results)

