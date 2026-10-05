from __future__ import annotations

import re
from pathlib import Path

from langchain_core.documents import Document

from .document_splitter import ContentUnit, split_content_units


_TXT_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百0-9]+[章节篇部].*|"
    r"\d+(?:\.\d+)*\s+\S.*)$"
)
_QUESTION = re.compile(r"^(?:Q\d+(?!\d)|Q\s*[:：]|问\s*[:：])", re.I)
_MD_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:\s+(.+?)\s*|\s*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")


def split_txt(
    file_path: str | Path, *, document_name: str | None = None,
) -> list[Document]:
    path = Path(file_path).resolve()
    text = _read_text(path)
    units: list[ContentUnit] = []
    headings: dict[int, str] = {}
    lines: list[str] = []
    kind = "paragraph"

    def flush() -> None:
        if lines and any(line.strip() for line in lines):
            units.append(ContentUnit(
                kind, "\n".join(lines).strip(), tuple(headings.values()),
                {"parser": "text"},
            ))
        lines.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            len(line) <= 60 and _TXT_HEADING.match(line)
            and not line.endswith(tuple("。！？!?；;"))
        ):
            flush()
            number = re.match(r"\d+(?:\.\d+)*", line)
            level = number.group().count(".") + 1 if number else 1
            _set_heading(headings, level, line)
            kind = "paragraph"
        elif _QUESTION.match(line):
            flush()
            kind = "faq"
            lines.append(line)
        elif _LIST.match(raw_line) and kind != "faq":
            flush()
            kind = "list"
            lines.append(line)
        elif not line:
            if kind in {"paragraph", "list"}:
                flush()
                kind = "paragraph"
            elif lines and lines[-1]:
                lines.append("")
        else:
            if kind == "list" and not raw_line.startswith((" ", "\t")):
                flush()
                kind = "paragraph"
            lines.append(line)
    flush()
    if not units and headings:
        units.append(ContentUnit(
            "heading", "\n".join(headings.values()), tuple(headings.values()),
            {"parser": "text"},
        ))
    return split_content_units(
        units, path=path, document_name=document_name or path.stem,
        chunk_size=500, chunk_overlap=50,
    )


def split_markdown(
    file_path: str | Path, *, document_name: str | None = None,
) -> list[Document]:
    path = Path(file_path).resolve()
    return split_content_units(
        collect_markdown_units(_read_text(path)), path=path,
        document_name=document_name or path.stem,
    )


def collect_markdown_units(text: str) -> list[ContentUnit]:
    """先识别 Markdown 块，再切长内容；代码内不识别标题和表格。"""
    lines = text.splitlines()
    units: list[ContentUnit] = []
    headings: dict[int, str] = {}
    paragraph: list[str] = []

    def add(kind: str, content: str | list[str], **metadata: str) -> None:
        units.append(ContentUnit(
            kind, content, tuple(headings.values()),
            {"parser": "markdown", **metadata},
        ))

    def flush() -> None:
        if paragraph:
            add("paragraph", "\n".join(paragraph))
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        fence_match = _FENCE.match(line)
        if fence_match:
            flush()
            fence, language = fence_match.groups()
            code: list[str] = []
            index += 1
            closing = re.compile(
                rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$"
            )
            while index < len(lines) and not closing.match(lines[index]):
                code.append(lines[index])
                index += 1
            add("code", "\n".join(code) + ("\n" if code else ""),
                code_language=language.strip(),
                code_fence=fence)
            index += 1
            continue

        heading = _MD_HEADING.match(line)
        setext = (
            index + 1 < len(lines) and line.strip()
            and re.fullmatch(r" {0,3}(?:=+|-+)\s*", lines[index + 1])
            and not _LIST.match(line)
        )
        if heading or setext:
            flush()
            if heading:
                level = len(heading.group(1))
                title = re.sub(r"\s+#+\s*$", "", heading.group(2) or "")
            else:
                level = 1 if lines[index + 1].lstrip().startswith("=") else 2
                title = line.strip()
                index += 1
            _set_heading(headings, level, title)
            index += 1
            continue

        if _is_table_start(lines, index):
            flush()
            rows = ["|".join(_table_cells(line))]
            index += 2  # 表头分隔线不作为数据行。
            while index < len(lines) and len(_table_cells(lines[index])) > 1:
                rows.append("|".join(_table_cells(lines[index])))
                index += 1
            add("table", rows)
            continue

        if _LIST.match(line):
            flush()
            item = [line]
            index += 1
            while index < len(lines):
                continuation = lines[index]
                if (
                    not continuation.strip() or _LIST.match(continuation)
                    or _MD_HEADING.match(continuation) or _FENCE.match(continuation)
                    or not continuation.startswith((" ", "\t"))
                ):
                    break
                item.append(continuation)
                index += 1
            add("list", "\n".join(item))
            continue

        if not line.strip():
            flush()
        else:
            paragraph.append(line)
        index += 1
    flush()
    if not units and headings:
        add("heading", "\n".join(headings.values()))
    return units


def _read_text(path: Path) -> str:
    # utf-8-sig 同时兼容有 BOM 和无 BOM 的 UTF-8 文件；不改代码缩进。
    text = path.read_text(encoding="utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def _set_heading(headings: dict[int, str], level: int, title: str) -> None:
    for depth in list(headings):
        if depth >= level:
            del headings[depth]
    if title:
        headings[level] = title


def _table_cells(line: str) -> list[str]:
    """分隔单元格时保留转义竖线与行内代码中的竖线。"""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells: list[str] = []
    current: list[str] = []
    code_delimiter = ""
    for token in re.findall(r"\\.|`+|[^\\`]|\\$", line):
        if token.startswith("`"):
            if not code_delimiter:
                code_delimiter = token
            elif token == code_delimiter:
                code_delimiter = ""
        if token == "|" and not code_delimiter:
            cells.append("".join(current).strip())
            current.clear()
        else:
            current.append(token)
    cells.append("".join(current).strip())
    return cells


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    cells = _table_cells(lines[index])
    separators = _table_cells(lines[index + 1])
    return (
        len(cells) > 1 and len(cells) == len(separators)
        and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separators)
    )
