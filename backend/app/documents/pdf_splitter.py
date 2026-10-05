from __future__ import annotations

import math
import hashlib
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from .document_splitter import (
    document_type_from_name,
    split_large_table,
    split_rule_text,
    is_rule_introduction,
)
from .chunk_quality import annotate_chunks, classify_chunk
from .ocr_review import apply_reviewed_ocr


OcrPage = Callable[[Path, int], str]

_HEADING_PATTERN = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百千万0-9]+[章节篇部]|"
    r"[0-9]+(?:\.[0-9]+){0,3}[、.\s]+"
    r")\S+"
)
_LIST_PATTERN = re.compile(
    r"^(?:[-•·]|\(?[0-9一二三四五六七八九十]+[、.)）])"
)
_PAGE_NUMBER_PATTERN = re.compile(
    r"^(?:第\s*\d+\s*页|[-—–]\s*\d+\s*[-—–]|"
    r"page\s+\d+(?:\s+of\s+\d+)?|虚构测试资料\s+第\s*\d+\s*页)$",
    flags=re.IGNORECASE,
)
_TERMINAL_PUNCTUATION = tuple("。！？!?；;：:")
_HEADING_MARKER_PATTERN = re.compile(
    r"^\[\[PDF_HEADING_(\d+)]](.*)$"
)


class PdfOcrUnavailableError(RuntimeError):
    """PDF needs OCR but the local OCR runtime is unavailable."""


@dataclass
class ExtractedPage:
    number: int
    text: str
    parser: str
    tables: list[list[str]] = field(default_factory=list)


@dataclass
class PdfUnit:
    kind: str
    content: str | list[str]
    section_path: tuple[str, ...]
    page_start: int
    page_end: int
    parsers: tuple[str, ...]
    rule_intro: str = ""


def split_pdf(
    file_path: str | Path,
    *,
    document_name: str | None = None,
    ocr_page: OcrPage | None = None,
) -> list[Document]:
    """Split a PDF while retaining page, section, and layout context."""
    path = Path(file_path).resolve()
    resolved_document_name = document_name or path.stem
    pages = extract_pdf_pages(path, ocr_page=ocr_page)
    corrections = {}
    if any(page.parser == 'ocr' for page in pages):
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        for page in pages:
            if page.parser == 'ocr':
                page.text, corrections[page.number] = apply_reviewed_ocr(page.text, file_hash=file_hash, page=page.number)
    pages = remove_repeated_marginalia(pages, document_name=resolved_document_name)
    units = collect_pdf_units(pages)

    normal_splitter = RecursiveCharacterTextSplitter(
        separators=[
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
        chunk_size=450,
        chunk_overlap=60,
    )
    chunks: list[Document] = []
    document_type = document_type_from_name(
        resolved_document_name
    )

    for unit_index, unit in enumerate(units):
        section_path = " > ".join(unit.section_path)
        section_name = section_path or "文档说明"
        page_label = (
            str(unit.page_start)
            if unit.page_start == unit.page_end
            else f"{unit.page_start}-{unit.page_end}"
        )
        prefix = (
            f"文档：{resolved_document_name}\n"
            f"章节：{section_name}\n"
            f"页码：{page_label}\n"
        )

        if unit.kind == "table":
            parts = split_large_table(
                list(unit.content),
                max_chars=900,
            )
        else:
            parts = split_rule_text(str(unit.content), normal_splitter, rule_intro=unit.rule_intro)

        parent_key = (
            f"pdf:{section_name}:{unit.page_start}:"
            f"{unit.page_end}:{unit_index}"
        )
        parser = "+".join(sorted(set(unit.parsers)))

        for part_index, part in enumerate(parts):
            if not part.strip():
                continue
            chunks.append(
                Document(
                    page_content=f"{prefix}\n{part.strip()}",
                    metadata={
                        "source": str(path),
                        "document_name": resolved_document_name,
                        "document_type": document_type,
                        "section_path": section_path,
                        "chunk_type": unit.kind,
                        "parent_key": parent_key,
                        "chunk_in_parent": part_index,
                        "page_start": unit.page_start,
                        "page_end": unit.page_end,
                        "parser": parser,
                        **({"rule_intro": unit.rule_intro} if unit.rule_intro else {}),
                    },
                )
            )

    for chunk in chunks:
        applied = [fix for number, fixes in corrections.items()
                   if chunk.metadata['page_start'] <= number <= chunk.metadata['page_end']
                   for fix in fixes if fix['after'] in chunk.page_content]
        if applied:
            chunk.metadata['ocr_corrections'] = applied
    # A corrected name is not certification of all other OCR fields.
    return annotate_chunks(chunks)


def extract_pdf_pages(
    file_path: str | Path,
    *,
    ocr_page: OcrPage | None = None,
) -> list[ExtractedPage]:
    path = Path(file_path).resolve()
    reader = PdfReader(str(path))
    layout_pages = _extract_layout_pages(path)
    extracted: list[ExtractedPage] = []

    for page_index, page in enumerate(reader.pages):
        page_number = page_index + 1
        layout_page = layout_pages.get(page_number)
        text = (
            layout_page.text
            if layout_page is not None
            else (page.extract_text() or "")
        )
        tables = (
            layout_page.tables
            if layout_page is not None
            else []
        )
        parser = (
            layout_page.parser
            if layout_page is not None
            else "pypdf"
        )

        if (
            _meaningful_character_count(text)
            < _ocr_min_characters()
            and _page_has_images(page)
        ):
            text = _run_ocr(
                path,
                page_number,
                ocr_page=ocr_page,
            )
            parser = "ocr"

        text = normalize_pdf_text(text)
        if text or tables:
            extracted.append(
                ExtractedPage(
                    number=page_number,
                    text=text,
                    parser=parser,
                    tables=tables,
                )
            )

    return extracted


def normalize_pdf_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\x00", "").replace("\u00ad", "")
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)
    lines: list[str] = []

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = re.sub(r"[\t \u3000]+", " ", raw_line).strip()
        lines.append(line)

    return "\n".join(lines).strip()


def remove_repeated_marginalia(
    pages: list[ExtractedPage],
    *,
    document_name: str = "",
) -> list[ExtractedPage]:
    normalized_page_edges: list[set[str]] = []
    for page in pages:
        lines = [line for line in page.text.splitlines() if line]
        normalized_page_edges.append({
            _normalize_marginal_line(line)
            for line in lines[:2] + lines[-2:]
            if _safe_repeated_margin(line, document_name)
        })

    normalized_counts = Counter(
        line
        for page_edges in normalized_page_edges
        for line in page_edges
        if line
    )
    required = max(2, math.ceil(len(pages) * 0.6))
    repeated = {
        line
        for line, count in normalized_counts.items()
        if count >= required
    }
    cleaned: list[ExtractedPage] = []
    for page in pages:
        lines = page.text.splitlines()
        nonempty_indexes = [
            index for index, line in enumerate(lines) if line
        ]
        edge_indexes = set(nonempty_indexes[:2]) | set(
            nonempty_indexes[-2:]
        )
        footer_indexes = set(nonempty_indexes[-2:])
        kept = [
            line
            for index, line in enumerate(lines)
            if not (
                (index in edge_indexes and _safe_repeated_margin(line, document_name)
                 and _normalize_marginal_line(line) in repeated)
                or (index in footer_indexes and _PAGE_NUMBER_PATTERN.fullmatch(line.strip())
                    and int(re.search(r"\d+", line).group()) == page.number)
            )
        ]
        cleaned.append(
            ExtractedPage(
                number=page.number,
                text="\n".join(kept).strip(),
                parser=page.parser,
                tables=page.tables,
            )
        )
    return cleaned


def collect_pdf_units(
    pages: list[ExtractedPage],
) -> list[PdfUnit]:
    units: list[PdfUnit] = []
    headings: list[str] = []
    paragraph_lines: list[str] = []
    paragraph_start = 0
    paragraph_end = 0
    paragraph_parsers: set[str] = set()

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        units.append(
            PdfUnit(
                kind="paragraph",
                content=_join_wrapped_lines(paragraph_lines),
                section_path=tuple(headings),
                page_start=paragraph_start,
                page_end=paragraph_end,
                parsers=tuple(sorted(paragraph_parsers)),
            )
        )
        paragraph_lines = []
        paragraph_parsers.clear()

    for page in pages:
        lines = page.text.splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                flush_paragraph()
                continue

            heading_level = _heading_level(line)
            if heading_level is not None:
                flush_paragraph()
                line = _strip_heading_marker(line)
                headings = headings[: heading_level - 1]
                headings.append(line)
                continue

            if _LIST_PATTERN.match(line):
                flush_paragraph()
                units.append(
                    PdfUnit(
                        kind="list",
                        content=line,
                        section_path=tuple(headings),
                        page_start=page.number,
                        page_end=page.number,
                        parsers=(page.parser,),
                    )
                )
                continue

            if not paragraph_lines:
                paragraph_start = page.number
            paragraph_end = page.number
            paragraph_parsers.add(page.parser)
            paragraph_lines.append(line)

            if line.endswith(_TERMINAL_PUNCTUATION):
                flush_paragraph()

        for table in page.tables:
            flush_paragraph()
            if table:
                units.append(
                    PdfUnit(
                        kind="table",
                        content=table,
                        section_path=tuple(headings),
                        page_start=page.number,
                        page_end=page.number,
                        parsers=(page.parser,),
                    )
                )

    flush_paragraph()
    return _coalesce_units(_bind_pdf_rule_lists(units))


def _bind_pdf_rule_lists(units: list[PdfUnit]) -> list[PdfUnit]:
    results: list[PdfUnit] = []
    for unit in units:
        previous = results[-1] if results else None
        if (previous and unit.kind == "list" and previous.section_path == unit.section_path
                and (previous.kind == "rule_group" or (
                    previous.kind == "paragraph" and is_rule_introduction(str(previous.content))))):
            results[-1] = PdfUnit(
                "rule_group", f"{previous.content}\n{unit.content}", previous.section_path,
                previous.page_start, unit.page_end,
                tuple(sorted(set(previous.parsers + unit.parsers))),
                previous.rule_intro or str(previous.content),
            )
        else:
            results.append(unit)
    return results


def _coalesce_units(units: Iterable[PdfUnit]) -> list[PdfUnit]:
    results: list[PdfUnit] = []
    for unit in units:
        if not results or unit.kind in {"table", "rule_group"}:
            results.append(unit)
            continue

        previous = results[-1]
        if (
            previous.kind != unit.kind
            or previous.kind == "rule_group"
            or previous.section_path != unit.section_path
            or classify_chunk(str(previous.content), {"section_path": " > ".join(previous.section_path)})
            != classify_chunk(str(unit.content), {"section_path": " > ".join(unit.section_path)})
            or len(str(previous.content)) + len(str(unit.content)) > 1800
        ):
            results.append(unit)
            continue

        results[-1] = PdfUnit(
            kind=previous.kind,
            content=(
                f"{previous.content}\n{unit.content}"
            ),
            section_path=previous.section_path,
            page_start=previous.page_start,
            page_end=unit.page_end,
            parsers=tuple(
                sorted(set(previous.parsers + unit.parsers))
            ),
            rule_intro=previous.rule_intro,
        )
    return results


def _extract_layout_pages(path: Path) -> dict[int, ExtractedPage]:
    if not _env_enabled("PDF_LAYOUT_ENABLED", default=True):
        return {}
    try:
        import pdfplumber
    except ImportError:
        return {}

    pages: dict[int, ExtractedPage] = {}
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page_number, page in enumerate(pdf.pages, start=1):
                tables: list[list[str]] = []
                table_boxes: list[
                    tuple[float, float, float, float]
                ] = []
                try:
                    found_tables = page.find_tables()
                except Exception:
                    found_tables = []

                for table in found_tables:
                    rows = _normalize_table_rows(table.extract())
                    if len(rows) >= 2:
                        tables.append(rows)
                        table_boxes.append(table.bbox)

                text_page = page
                if table_boxes:
                    text_page = page.filter(
                        lambda obj: not any(
                            _object_inside_box(obj, box)
                            for box in table_boxes
                        )
                    )

                text = _extract_reading_order_text(text_page)
                pages[page_number] = ExtractedPage(
                    number=page_number,
                    text=text,
                    parser=(
                        "pdfplumber-layout"
                        if tables or _looks_multicolumn(text_page)
                        else "pdfplumber"
                    ),
                    tables=tables,
                )
    except Exception:
        # Layout extraction is enrichment. Malformed layout data should
        # still get a chance to use pypdf's simpler text extraction.
        return {}
    return pages


def _extract_reading_order_text(page) -> str:
    try:
        words = page.extract_words(
            use_text_flow=False,
            keep_blank_chars=False,
            extra_attrs=["size"],
        )
    except Exception:
        return page.extract_text() or ""
    if not words:
        return ""

    sizes = [
        float(word["size"])
        for word in words
        if word.get("size") is not None
    ]
    body_size = median(sizes) if sizes else 0.0
    columns = _split_word_columns(words, float(page.width))
    rendered: list[str] = []
    for column in columns:
        lines: list[list[dict]] = []
        for word in sorted(column, key=lambda item: (item["top"], item["x0"])):
            if not lines or abs(lines[-1][0]["top"] - word["top"]) > 3:
                lines.append([word])
            else:
                lines[-1].append(word)
        for line in lines:
            text = " ".join(
                str(word["text"])
                for word in sorted(line, key=lambda item: item["x0"])
            )
            line_size = max(
                (
                    float(word.get("size") or 0)
                    for word in line
                ),
                default=0.0,
            )
            if (
                body_size > 0
                and line_size >= body_size * 1.25
                and len(text) <= 60
            ):
                text = f"[[PDF_HEADING_1]]{text}"
            rendered.append(text)
    return "\n".join(rendered)


def _split_word_columns(
    words: list[dict],
    page_width: float,
) -> list[list[dict]]:
    centers = sorted(
        (float(word["x0"]) + float(word["x1"])) / 2
        for word in words
    )
    if len(centers) < 4:
        return [words]
    gaps = [
        (right - left, (right + left) / 2)
        for left, right in zip(centers, centers[1:])
    ]
    largest_gap, divider = max(gaps)
    if largest_gap < page_width * 0.12:
        return [words]
    left = [
        word
        for word in words
        if (
            float(word["x0"]) + float(word["x1"])
        ) / 2 < divider
    ]
    right = [word for word in words if word not in left]
    if len(left) < 2 or len(right) < 2:
        return [words]
    left_lines = {
        round(float(word["top"]) / 4)
        for word in left
    }
    right_lines = {
        round(float(word["top"]) / 4)
        for word in right
    }
    if len(left_lines & right_lines) < 2:
        return [words]
    return [left, right]


def _looks_multicolumn(page) -> bool:
    try:
        words = page.extract_words()
    except Exception:
        return False
    return len(_split_word_columns(words, float(page.width))) > 1


def _normalize_table_rows(rows: list[list[str | None]]) -> list[str]:
    normalized: list[str] = []
    for row in rows:
        cells = [
            re.sub(r"\s+", " ", cell or "").strip()
            for cell in row
        ]
        if any(cells):
            normalized.append("|".join(cells))
    return normalized


def _object_inside_box(
    obj: dict,
    box: tuple[float, float, float, float],
) -> bool:
    x0, top, x1, bottom = box
    center_x = (float(obj.get("x0", 0)) + float(obj.get("x1", 0))) / 2
    center_y = (float(obj.get("top", 0)) + float(obj.get("bottom", 0))) / 2
    return x0 <= center_x <= x1 and top <= center_y <= bottom


def _run_ocr(
    path: Path,
    page_number: int,
    *,
    ocr_page: OcrPage | None,
) -> str:
    if ocr_page is not None:
        return ocr_page(path, page_number)
    if not _env_enabled("PDF_OCR_ENABLED", default=True):
        return ""

    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise PdfOcrUnavailableError(
            "PDF 页面没有可提取文本，需要 OCR；请安装 "
            "pytesseract、pdf2image、Poppler 和 Tesseract。"
        ) from exc

    tesseract_command = os.getenv("TESSERACT_CMD", "").strip()
    if tesseract_command:
        pytesseract.pytesseract.tesseract_cmd = tesseract_command
    poppler_path = os.getenv("POPPLER_PATH", "").strip() or None

    try:
        images = convert_from_path(
            str(path),
            dpi=220,
            first_page=page_number,
            last_page=page_number,
            poppler_path=poppler_path,
            fmt="png",
            thread_count=1,
        )
        if not images:
            return ""
        return pytesseract.image_to_string(
            images[0],
            lang=os.getenv("PDF_OCR_LANGUAGE", "chi_sim+eng"),
        )
    except Exception as exc:
        raise PdfOcrUnavailableError(
            f"PDF 第 {page_number} 页 OCR 失败：{exc}"
        ) from exc


def _heading_level(line: str) -> int | None:
    marker_match = _HEADING_MARKER_PATTERN.match(line)
    marker_level: int | None = None
    if marker_match is not None:
        marker_level = max(1, int(marker_match.group(1)))
        line = marker_match.group(2).strip()
    if len(line) > 60:
        return None
    if not _HEADING_PATTERN.match(line):
        return marker_level
    if line.startswith("第"):
        return 1
    match = re.match(r"^(\d+(?:\.\d+)*)", line)
    if match is None:
        return 1
    return match.group(1).count(".") + 1


def _strip_heading_marker(line: str) -> str:
    match = _HEADING_MARKER_PATTERN.match(line)
    if match is None:
        return line
    return match.group(2).strip()


def _join_wrapped_lines(lines: list[str]) -> str:
    result = ""
    for line in lines:
        if not result:
            result = line
            continue
        separator = " " if result[-1:].isascii() and line[:1].isascii() else ""
        result = f"{result}{separator}{line}"
    return result


def _normalize_marginal_line(line: str) -> str:
    return re.sub(r"\s+", "", line.strip().lower())


def _safe_repeated_margin(line: str, document_name: str = "") -> bool:
    # Repetition alone cannot prove a rule is a header/footer. Do not collapse
    # changing amounts/versions into the same line or remove repeated obligations.
    if len(line) > 80 or re.search(
        r"\d|[|。！？；;:：]|必须|应当|不得|禁止|限额|上限|触发|应在|需要|条件|例外|"
        r"生效|失效|有效期|版本|审批人|负责人|签署|验收结果|不应|不能|不可|不自动|"
        r"仅|只有|如果|若|允许|可以|须|要求|不代表|不证明|确认|认定|已|重试|幂等", line,
    ):
        return False
    if classify_chunk(line, {"document_name": document_name, "section_path": ""}) == "front_matter":
        return True
    # A repeated title may have a short company prefix absent from the filename.
    # Unknown short business lines are not headers just because they repeat.
    title = re.sub(r"\.(?:pdf|docx?|md|txt)$", "", document_name, flags=re.I)
    title = _normalize_marginal_line(re.sub(r"^\d+[_、.\s-]+", "", title))
    normalized = _normalize_marginal_line(line)
    prefix = normalized[:-len(title)] if title and normalized.endswith(title) else ""
    return bool(prefix and re.fullmatch(r"[\w·&-]+(?:科技|公司|有限公司)", prefix))


def _meaningful_character_count(text: str) -> int:
    return len(re.sub(r"\W+", "", text, flags=re.UNICODE))


def _ocr_min_characters() -> int:
    raw_value = os.getenv("PDF_OCR_MIN_CHARACTERS", "20")
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 20


def _page_has_images(page) -> bool:
    try:
        return bool(page.images)
    except Exception:
        resources = page.get("/Resources") or {}
        return bool(resources.get("/XObject"))


def _env_enabled(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
