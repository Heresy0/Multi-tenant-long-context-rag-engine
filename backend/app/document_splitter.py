from __future__ import annotations

import re
from dataclasses import dataclass
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

FAQ_PATTERN = re.compile(r"^Q\d+\s*")
NUMBERED_HEADING_PATTERN = re.compile(
    r"^(\d+(?:\.\d+)*)\s+\S"
)


@dataclass
class ContentUnit:
    kind: str
    content: str | list[str]
    section_path: tuple[str,...]


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
            "".join(cell.text.split())
            for cell in row.cells
        ]
        if any(cells):
            rows.append("|".join(cells))

    return rows


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
            text = f"-{text}"

        paragraphs.append(text)

    flush_paragraphs()
    flush_faq()

    return units


def split_docx(file_path: str | Path) -> list[Document]:
    path = Path(file_path).resolve()
    word_document = WordDocument(path)

    document_name = path.stem
    document_type = document_type_from_name(document_name)
    units = collect_units(word_document)

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
        chunk_size =450,
        chunk_overlap=60,
    )

    faq_splitter = RecursiveCharacterTextSplitter(
        separators=["\n", "。", "；", "，", ""],
        chunk_size=900,
        chunk_overlap=80,
    )

    chunks: list[Document] = []

    for unit_index, unit in enumerate(units):
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
        else:
            parts = normal_splitter.split_text(str(unit.content))

        parent_key = f"{section_name}:{unit_index}"

        for part_index, part in enumerate(parts):
            chunks.append(
                Document(
                    page_content=f"{prefix}\n{part}",
                    metadata={
                        "source": str(path),
                        "document_name": document_name,
                        "document_type": document_type,
                        "section_path": section_path,
                        "chunk_type": unit.kind,
                        "parent_key": parent_key,
                        "chunk_in_parent": part_index,
                    },
                )
            )

    return chunks

    
