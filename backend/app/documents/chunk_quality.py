"""Shared, conservative chunk classification and ingestion quality metadata.

These hints do not certify OCR accuracy, decide permissions, or erase content.
Unknown and mixed business prose always remains usable content.
"""
import re
import unicodedata

from langchain_core.documents import Document


CLEANING_VERSION = "conservative-v1"
_FIELDS = {
    "文档编号", "版本", "版本号", "生效日期", "失效日期", "复审日期", "状态",
    "适用范围", "密级", "归口部门", "入库范围", "可见范围", "编制部门",
    "公司名称", "文档名称", "作者", "发布日期",
}
_OPERATIVE = re.compile(r"必须|应当|不得|禁止|限额|上限|触发|待核查|应在|需要")
_WRAPPER = re.compile(
    r"^文档:[^\n]*\n章节:[^\n]*\n(?:页码:\d+(?:-\d+)?\n)?[ \t]*\n"
)


def chunk_body(text: str) -> str:
    """Strip only our generated wrapper; never delete original labelled prose."""
    normalized = unicodedata.normalize("NFKC", text).strip()
    return _WRAPPER.sub("", normalized, count=1)


def _title(value: str) -> str:
    value = re.sub(r"\.(?:pdf|docx?|md|txt)$", "", value, flags=re.I)
    value = re.sub(r"^\d+[_、.\s-]+", "", value)
    return "".join(value.split())


def _metadata_line(line: str) -> bool:
    parts = [part.strip() for part in re.split(r"[;；]", line) if part.strip()]
    for part in parts:
        cells = [cell.strip() for cell in re.split(r"[:|]", part)]
        if (len(cells) < 2 or len(cells) % 2
                or any(cells[i] not in _FIELDS or not cells[i + 1]
                       or len(cells[i + 1]) > 160 for i in range(0, len(cells), 2))):
            return False
    return bool(parts)


def classify_chunk(text: str, metadata: dict) -> str:
    # Recompute from content, including legacy chunks and stale stored hints.
    # Code and unknown tables must not be demoted just because of their words.
    if metadata.get("chunk_type") == "code":
        return "content"
    lines = [line.strip() for line in chunk_body(text).splitlines() if line.strip()]
    if not lines:
        return "front_matter"
    title = _title(unicodedata.normalize("NFKC", str(metadata.get("document_name", ""))))
    preamble = metadata.get("section_path") in (
        None, "", "文档说明", "未标注章节", "封面", "基本信息", "文档信息",
    )
    for line in lines:
        if title and _title(line) == title:
            continue
        if _OPERATIVE.search(line):
            return "content"
        if _metadata_line(line):
            continue
        if (preamble and len(line) <= 80 and re.fullmatch(
                r"[\w ()·&-]+(?:有限公司|有限责任公司)(?:\s*\|\s*内部资料)?", line)):
            continue
        if (re.match(r"(?:免责声明[: ]|虚构测试资料|本(?:文档|资料)仅用于)", line)
                and re.search(r"仅用于|不具有.*效力", line)):
            continue
        return "content"
    return "front_matter"


def annotate_chunks(chunks: list[Document]) -> list[Document]:
    for chunk in chunks:
        ocr = "ocr" in str(chunk.metadata.get("parser", "")).lower().split("+")
        chunk.metadata.update(
            evidence_role=classify_chunk(chunk.page_content, chunk.metadata),
            cleaning_version=CLEANING_VERSION,
            quality={
                "review_required": ocr,
                "review_reasons": ["ocr_unverified"] if ocr else [],
            },
        )
    return chunks


def summarize_quality(chunks: list[Document]) -> dict:
    ocr_count = sum(
        "ocr_unverified" in chunk.metadata.get("quality", {}).get("review_reasons", [])
        for chunk in chunks
    )
    return {
        "cleaning_version": CLEANING_VERSION,
        "front_matter_chunks": sum(chunk.metadata.get("evidence_role") == "front_matter" for chunk in chunks),
        "ocr_chunks": ocr_count,
        "review_required": bool(ocr_count),
        "review_reasons": ["ocr_unverified"] if ocr_count else [],
    }
