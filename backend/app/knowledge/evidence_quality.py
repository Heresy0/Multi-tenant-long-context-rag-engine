"""Conservative front-matter detection; unknown prose remains usable evidence.

These are ordering hints, not relevance scores or permission decisions. Never
discard a chunk just because it has no heading or belongs to 文档说明.
"""
import re
import unicodedata

from langchain_core.documents import Document


_FIELDS = {
    "文档编号", "版本", "版本号", "生效日期", "失效日期", "复审日期", "状态",
    "适用范围", "密级", "归口部门", "入库范围", "可见范围", "编制部门",
    "公司名称", "文档名称", "作者", "发布日期",
}
_OPERATIVE = re.compile(r"必须|应当|不得|禁止|限额|上限|触发|待核查|应在|需要")
_METADATA_QUERY = re.compile(
    r"(?:文档编号|版本号|版本|生效日期|失效日期|复审日期|有效期|适用范围|"
    r"归口部门|编制部门|密级|公司名称|文档名称|标题|免责声明)"
    r"\s*(?:是什么|是多少|是哪天|是何时|是什么时候|有哪些|是什么内容|为何|如何规定|"
    r"是谁|是哪个(?:部门)?|叫什么(?:名字)?|怎么写)"
    r"[?？。!！]*\s*$"
)


def _title(value: str) -> str:
    value = re.sub(r"\.(?:pdf|docx?|md|txt)$", "", value, flags=re.I)
    value = re.sub(r"^\d+[_、.\s-]+", "", value)
    return "".join(value.split())


def _metadata_line(line: str) -> bool:
    # Covers labelled paragraphs and the splitter's key/value table rows.
    parts = [part.strip() for part in re.split(r"[;；]", line) if part.strip()]
    for part in parts:
        cells = [cell.strip() for cell in re.split(r"[:|]", part)]
        if (len(cells) < 2 or len(cells) % 2
                or any(cells[i] not in _FIELDS or not cells[i + 1]
                       or len(cells[i + 1]) > 160 for i in range(0, len(cells), 2))):
            return False
    return bool(parts)


def evidence_role(document: Document) -> str:
    """Return front_matter only for positively recognized non-operative content."""
    text = unicodedata.normalize("NFKC", document.page_content).strip()
    # Ignore the known splitter-generated wrapper, not arbitrary original lines.
    text = re.sub(r"^文档:[^\n]*\n章节:[^\n]*\n\s*\n", "", text, count=1)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "front_matter"
    title = _title(unicodedata.normalize("NFKC", str(document.metadata.get("document_name", ""))))
    preamble = document.metadata.get("section_path") in (None, "", "文档说明", "未标注章节", "封面", "基本信息", "文档信息")
    for line in lines:
        if title and _title(line) == title:
            continue
        if _OPERATIVE.search(line):
            return "content"
        if _metadata_line(line):
            continue
        if (preamble and len(line) <= 80 and re.fullmatch(r"[\w ()·&-]+(?:有限公司|有限责任公司)", line)):
            continue
        if (re.match(r"(?:免责声明[: ]|虚构测试资料|本(?:文档|资料)仅用于)", line)
                and re.search(r"仅用于|不具有.*效力", line)):
            continue
        return "content"
    return "front_matter"


def prefer_content(question: str) -> bool:
    """Keep the original ranking for explicit document-attribute questions."""
    return not bool(_METADATA_QUERY.search(question))


def prioritize_content(documents: list[Document], *, question: str = "") -> list[Document]:
    if not prefer_content(question):
        return documents
    return sorted(documents, key=lambda doc: evidence_role(doc) == "front_matter")
