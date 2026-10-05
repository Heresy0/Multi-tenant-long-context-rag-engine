from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from docx import Document as WordDocument
from docx.document import Document as WordDocumentType
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .chunk_quality import annotate_chunks

FAQ_PATTERN = re.compile(r"^Q\d+\s*")
NUMBERED_HEADING_PATTERN = re.compile(
    r"^(\d+(?:\.\d+)*)\s+\S"
)
_RULE_INTRO = re.compile(r"条件|仅当|只有|如果|若|必须|应当|不得|禁止|除外|例外")


@dataclass
class ContentUnit:
    kind: str
    content: str | list[str]
    section_path: tuple[str,...]
    metadata: dict[str, str] = field(default_factory=dict)


def iter_docx_blocks(
        document: WordDocumentType,
) -> Iterator[Paragraph | Table]:
    """按照Word中的真实顺序遍历段落和表格。"""
    for child in document.element.body.iterchildren():
        if isinstance(child,CT_P):
            yield Paragraph(child,document)
        elif isinstance(child,CT_Tbl):
            yield Table(child,document)


def get_heading_level(paragraph: Paragraph) -> int | None:
    text = paragraph.text.strip()
    if not text:
        return None

    style_name = paragraph.style.name or ""

    match = re.search(
        r"(?:Heading|标题)\s*(\d+)",
        style_name,
        flags=re.IGNORECASE,
    )
    if match:
        return int(match.group(1))

    #兼容“1 数据分级”“3.1 紧急变更”一类标题
    match = NUMBERED_HEADING_PATTERN.match(text)

    if match and len(text) <= 50:
        return match.group(1).count(".") + 1

    return None


def table_rows(table: Table) -> list[str]:
    rows: list[str] = []

    for row in table.rows:
        cells = [
            re.sub(r"\s+", " ", cell.text).strip()
            for cell in row.cells
        ]
        if any(cells):
            rows.append("|".join(cells))

    return rows


def bind_rule_lists(units: list[ContentUnit]) -> list[ContentUnit]:
    """Bind an explicit rule introduction to its following same-section list.

    No semantic guessing across headings or tables. Oversized groups still have
    a common parent and repeat the introduction when split.
    """
    results: list[ContentUnit] = []
    index = 0
    while index < len(units):
        unit = units[index]
        intro = str(unit.content).strip()
        if unit.kind == "paragraph" and is_rule_introduction(intro):
            following: list[str] = []
            cursor = index + 1
            while (cursor < len(units) and units[cursor].kind == "list"
                   and units[cursor].section_path == unit.section_path):
                following.append(str(units[cursor].content))
                cursor += 1
            if following:
                results.append(ContentUnit(
                    "rule_group", "\n".join([intro, *following]), unit.section_path,
                    {**unit.metadata, "rule_intro": intro},
                ))
                index = cursor
                continue
        results.append(unit)
        index += 1
    return results


def is_rule_introduction(text: str) -> bool:
    return len(text) <= 200 and text.endswith((":", "：")) and bool(_RULE_INTRO.search(text))


def split_rule_text(text: str, splitter: RecursiveCharacterTextSplitter, *,
                    rule_intro: str = "", max_rule_chars: int = 900) -> list[str]:
    """Keep short conditional clauses intact; bound expansion to 900 chars."""
    if len(text) <= max_rule_chars and _RULE_INTRO.search(text):
        return [text]
    if rule_intro and text.startswith(rule_intro):
        # The introduction identifies applicability, not a synthesized summary.
        body = text[len(rule_intro):].lstrip("\n")
        body_size = max_rule_chars - len(rule_intro) - 1
        bounded = RecursiveCharacterTextSplitter(
            separators=["\n\n", "\n", "。", "；", "，", " ", ""],
            chunk_size=body_size, chunk_overlap=min(60, body_size - 1),
        )
        return [f"{rule_intro}\n{part}" for part in bounded.split_text(body)]
    return splitter.split_text(text)


def split_large_table(
        rows: list[str],
        max_chars: int = 900,
) -> list[str]:
    if not rows:
        return []

    if len("\n".join(rows)) <= max_chars:
        return ["\n".join(rows)]

    #大表切分是，每个分块重复表头
    header = rows[0]
    results: list[str] = []
    current: list[str] = []
    current_size = len(header)

    for row in rows[1:]:
        if current and current_size + len(row) > max_chars:
            results.append("\n".join([header,*current]))
            current = []
            current_size = len(header)

        current.append(row)
        current_size += len(row) + 1

    if current:
        results.append("\n".join([header,*current]))

    return results


def document_type_from_name(name: str) -> str:
    if "FAQ" in name.upper() or "问答" in name:
        return "faq"
    if "合同" in name:
        return "contract"
    if "技术" in name or "API" in name.upper():
        return "technical"
    if "流程" in name or "手册" in name:
        return "procedure"
    if "制度" in name or "规定" in name:
        return "policy"
    return "general"


def collect_units(
        document: WordDocumentType,
) -> list[ContentUnit]:
    units: list[ContentUnit] = []
    headings: list[str] = []
    paragraphs: list[str] = []
    faq: list[str] | None = None

    def flush_paragraphs() -> None:
        nonlocal paragraphs
        if paragraphs:
            units.append(
                ContentUnit(
                    kind="paragraph",
                    content="\n".join(paragraphs),
                    section_path=tuple(headings),
                )
            )
            paragraphs = []

    def flush_faq() -> None:
        nonlocal faq
        if faq:
            units.append(
                ContentUnit(
                    kind="faq",
                    content="\n".join(faq),
                    section_path=tuple(headings),
                )
            )
            faq = None

    for block in iter_docx_blocks(document):
        if isinstance(block,Table):
            flush_paragraphs()
            flush_faq()

            rows = table_rows(block)
            if rows:
                units.append(
                    ContentUnit(
                        kind="table",
                        content=rows,
                        section_path=tuple(headings),
                    )
                )
            continue

        text = block.text.strip()
        if not text:
            continue

        heading_level = get_heading_level(block)

        if heading_level is not None:
            flush_paragraphs()
            flush_faq()

            headings = headings[:heading_level - 1]
            headings.append(text)
            continue

        if FAQ_PATTERN.match(text):
            flush_paragraphs()
            flush_faq()
            faq = [text]
            continue

        if faq is not None:
            faq.append(text)
            continue

        if block.style.name.startswith("List"):
            flush_paragraphs()
            units.append(ContentUnit("list", f"-{text}", tuple(headings)))
            continue

        paragraphs.append(text)

    flush_paragraphs()
    flush_faq()

    return units


def split_docx(
    file_path: str | Path,
    *,
    document_name: str | None = None,
) -> list[Document]:
    path = Path(file_path).resolve()
    word_document = WordDocument(path)

    resolved_document_name = (
        document_name or path.stem
    )
    units = collect_units(word_document)

    return split_content_units(
        units, path=path, document_name=resolved_document_name,
    )


def split_content_units(
    units: list[ContentUnit],
    *,
    path: Path,
    document_name: str,
    chunk_size: int = 450,
    chunk_overlap: int = 60,
) -> list[Document]:
    """各格式识别结构后，共用分块、上下文和元数据生成。"""
    document_type = document_type_from_name(document_name)

    normal_splitter = RecursiveCharacterTextSplitter(
        separators = [
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            "?",
            "；",
            "，",
            " ",
            "",
        ],
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    faq_splitter = RecursiveCharacterTextSplitter(
        separators=["\n", "。", "；", "，", ""],
        chunk_size=900,
        chunk_overlap=80,
    )

    chunks: list[Document] = []

    for unit_index, unit in enumerate(bind_rule_lists(units)):
        section_path = " > ".join(unit.section_path)
        section_name = section_path or "文档说明"

        prefix = (
            f"文档：{document_name}\n"
            f"章节：{section_name}\n"
        )

        if unit.kind == "table":
            parts = split_large_table(unit.content)
        elif unit.kind == "faq":
            parts = faq_splitter.split_text(str(unit.content))
        elif unit.kind == "code":
            parts = split_code_block(
                str(unit.content),
                language=unit.metadata.get("code_language", ""),
                fence=unit.metadata.get("code_fence", "```"),
            )
        else:
            parts = split_rule_text(
                str(unit.content), normal_splitter,
                rule_intro=unit.metadata.get("rule_intro", ""),
            )

        parent_key = f"{section_name}:{unit_index}"

        for part_index, part in enumerate(parts):
            chunks.append(
                Document(
                    page_content=f"{prefix}\n{part}",
                    metadata={
                        "source": str(path),
                        "document_name": (
                            document_name
                        ),
                        "document_type": document_type,
                        "section_path": section_path,
                        "chunk_type": unit.kind,
                        "parent_key": parent_key,
                        "chunk_in_parent": part_index,
                        "parser": unit.metadata.get("parser", "python-docx"),
                        **unit.metadata,
                    },
                )
            )

    return annotate_chunks(chunks)


def split_code_block(
    code: str, *, language: str, fence: str, max_chars: int = 900,
) -> list[str]:
    """优先按代码行切分，保留缩进，并为每个分块补完整围栏。"""
    parts: list[str] = []
    current = ""
    for line in code.splitlines(keepends=True):
        if current and len(current) + len(line) > max_chars:
            parts.append(current)
            current = ""
        # 极长单行也有限制；不使用文本切分器，避免丢失缩进。
        while len(line) > max_chars:
            parts.append(line[:max_chars])
            line = line[max_chars:]
        current += line
    if current or not parts:
        parts.append(current)
    wrapped: list[str] = []
    for part in parts:
        separator = "" if part.endswith("\n") else "\n"
        wrapped.append(f"{fence}{language}\n{part}{separator}{fence}")
    return wrapped
