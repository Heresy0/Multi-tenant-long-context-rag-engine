from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document


@dataclass(frozen=True)
class ContextItem:
    citation_id: str
    content: str
    source: str
    document_name: str
    section_path: str
    chunk_id: str | None
    rerank_score: float | None


@dataclass(frozen=True)
class BuiltContext:
    text: str
    items: list[ContextItem]
    total_characters: int

    @classmethod
    def empty(cls) -> "BuiltContext":
        return cls(
            text="",
            items=[],
            total_characters=0,
        )


class ContextBuilder:
    def __init__(
        self,
        *,
        max_characters: int = 12000,
        max_chunks_per_source: int = 4,
    ) -> None:
        if max_characters < 1:
            raise ValueError("max_characters必须大于0")

        if max_chunks_per_source < 1:
            raise ValueError(
                "max_chunks_per_source必须大于0"
            )

        self.max_characters = max_characters
        self.max_chunks_per_source = max_chunks_per_source

    def build(
        self,
        documents: list[Document],
    ) -> BuiltContext:
        if not documents:
            return BuiltContext.empty()

        documents = self._deduplicate(documents)
        items: list[ContextItem] = []
        rendered_blocks: list[str] = []
        source_counts: dict[str, int] = {}
        used_characters = 0

        for document in documents:
            content = document.page_content.strip()

            if not content:
                continue

            metadata = document.metadata
            source = str(metadata.get("source", "未知来源"))
            source_key = str(
                metadata.get("source_id") or source
            )
            count = source_counts.get(source_key, 0)

            if count >= self.max_chunks_per_source:
                continue

            document_name = str(
                metadata.get("document_name")
                or Path(source).stem
                or "未知文档"
            )
            section_path = str(
                metadata.get("section_path")
                or "未标注章节"
            )
            citation_id = f"资料{len(items) + 1}"
            prefix = (
                f"[{citation_id}]\n"
                f"文档：{document_name}\n"
                f"章节：{section_path}\n"
                "内容："
            )
            separator_size = 2 if rendered_blocks else 0
            available_content_size = (
                self.max_characters
                - used_characters
                - separator_size
                - len(prefix)
            )

            if available_content_size < 1:
                continue

            if len(content) > available_content_size:
                # 后续长块不能挤掉排名更高的已选内容。
                if rendered_blocks:
                    continue

                content = self._truncate_content(
                    content,
                    available_content_size,
                )

            if not content:
                continue

            chunk_id = (
                getattr(document, "id", None)
                or metadata.get("chunk_id")
                or metadata.get("chunk_content_hash")
            )
            rerank_score = metadata.get("rerank_score")
            item = ContextItem(
                citation_id=citation_id,
                content=content,
                source=source,
                document_name=document_name,
                section_path=section_path,
                chunk_id=(
                    str(chunk_id)
                    if chunk_id is not None
                    else None
                ),
                rerank_score=(
                    float(rerank_score)
                    if isinstance(rerank_score, (int, float))
                    and not isinstance(rerank_score, bool)
                    else None
                ),
            )
            rendered_block = f"{prefix}{content}"

            items.append(item)
            rendered_blocks.append(rendered_block)
            source_counts[source_key] = count + 1
            used_characters += separator_size + len(rendered_block)

        if not items:
            return BuiltContext.empty()

        context_text = "\n\n".join(rendered_blocks)

        return BuiltContext(
            text=context_text,
            items=items,
            total_characters=len(context_text),
        )

    def _deduplicate(
        self,
        documents: list[Document],
    ) -> list[Document]:
        results: list[Document] = []
        seen: set[str] = set()

        for document in documents:
            content = document.page_content.strip()

            if not content:
                continue

            key = str(
                document.metadata.get("chunk_content_hash")
                or self._content_hash(content)
            )

            if key in seen:
                continue

            seen.add(key)
            results.append(document)

        return results

    @staticmethod
    def _content_hash(content: str) -> str:
        normalized = re.sub(r"\s+", "", content).lower()

        return hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _truncate_content(
        content: str,
        max_characters: int,
    ) -> str:
        if max_characters < 1:
            return ""

        if len(content) <= max_characters:
            return content

        truncated = content[:max_characters]
        positions = [
            truncated.rfind(separator)
            for separator in ("。", "！", "？", "\n")
        ]
        safe_position = max(positions)

        if safe_position >= max_characters // 2:
            return truncated[: safe_position + 1]

        return truncated
