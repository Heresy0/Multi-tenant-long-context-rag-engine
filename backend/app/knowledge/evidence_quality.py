"""Conservative front-matter detection; unknown prose remains usable evidence.

These are ordering hints, not relevance scores or permission decisions. Never
discard a chunk just because it has no heading or belongs to 文档说明.
"""
import re

from langchain_core.documents import Document

from ..documents.chunk_quality import classify_chunk

_METADATA_QUERY = re.compile(
    r"(?:文档编号|版本号|版本|生效日期|失效日期|复审日期|有效期|适用范围|"
    r"归口部门|编制部门|密级|公司名称|文档名称|标题|免责声明)"
    r"\s*(?:是什么|是多少|是哪天|是何时|是什么时候|有哪些|是什么内容|为何|如何规定|"
    r"是谁|是哪个(?:部门)?|叫什么(?:名字)?|怎么写)"
    r"[?？。!！]*\s*$"
)


def evidence_role(document: Document) -> str:
    """Return front_matter only for positively recognized non-operative content."""
    return classify_chunk(document.page_content, document.metadata)


def prefer_content(question: str) -> bool:
    """Keep the original ranking for explicit document-attribute questions."""
    return not bool(_METADATA_QUERY.search(question))


def prioritize_content(documents: list[Document], *, question: str = "") -> list[Document]:
    if not prefer_content(question):
        return documents
    return sorted(documents, key=lambda doc: evidence_role(doc) == "front_matter")
