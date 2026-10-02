from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .answer_models import AnswerDraft
from .context_builder import BuiltContext


FACT_PATTERN = re.compile(
    r"""
    (?P<date>
        (?<!\d)
        \d{4}
        (?:
            年\d{1,2}月\d{1,2}日
            |
            [-/]\d{1,2}[-/]\d{1,2}
        )
        (?!\d)
    )
    |
    (?P<percent>
        (?<![\d.])
        -?\d+(?:,\d{3})*(?:\.\d+)?
        %
    )
    |
    (?P<number>
        (?<![\d.])
        -?\d+(?:,\d{3})*(?:\.\d+)?
        (?![\d.%])
    )
    """,
    flags=re.VERBOSE,
)


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(
        default_factory=list
    )


class AnswerValidator:
    def validate(
        self,
        *,
        draft: AnswerDraft,
        context: BuiltContext,
        question: str = "",
    ) -> ValidationResult:
        errors: list[str] = []

        allowed_ids = {
            item.citation_id
            for item in context.items
        }
        item_map = {
            item.citation_id: item
            for item in context.items
        }
        question_facts = self._extract_numeric_facts(
            question
        )

        if not draft.answerable:
            if draft.claims:
                errors.append(
                    "拒答结果不应同时包含确定性结论"
                )

            return ValidationResult(
                valid=not errors,
                errors=errors,
            )

        if not draft.claims:
            errors.append(
                "可回答结果至少需要一个结论"
            )

        for claim_index, claim in enumerate(
            draft.claims,
            start=1,
        ):
            if not claim.citations:
                errors.append(
                    f"第{claim_index}个结论没有引用"
                )
                continue

            invalid_ids = [
                citation_id
                for citation_id in claim.citations
                if citation_id not in allowed_ids
            ]

            if invalid_ids:
                errors.append(
                    f"第{claim_index}个结论包含"
                    f"不存在的引用：{invalid_ids}"
                )
                continue

            cited_text = "\n".join(
                item_map[citation_id].content
                for citation_id in claim.citations
            )

            claim_facts = self._extract_numeric_facts(
                claim.text
            )
            evidence_facts = self._extract_numeric_facts(
                cited_text
            )

            unsupported_facts = sorted(
                claim_facts
                - evidence_facts
                - question_facts
            )

            if unsupported_facts:
                readable_facts = [
                    self._display_fact(fact)
                    for fact in unsupported_facts
                ]

                errors.append(
                    f"第{claim_index}个结论中的数字或日期"
                    f"缺少证据支持：{readable_facts}"
                )

        return ValidationResult(
            valid=not errors,
            errors=errors,
        )

    @classmethod
    def _extract_numeric_facts(
        cls,
        text: str,
    ) -> set[str]:
        # 将全角数字、全角百分号等转换为半角形式
        normalized_text = unicodedata.normalize(
            "NFKC",
            text,
        )

        facts: set[str] = set()

        for match in FACT_PATTERN.finditer(
            normalized_text
        ):
            fact_type = match.lastgroup

            if fact_type is None:
                continue

            raw_value = match.group(fact_type)

            if fact_type == "date":
                facts.add(
                    cls._normalize_date(raw_value)
                )
            elif fact_type == "percent":
                number = raw_value.removesuffix("%")
                facts.add(
                    "percent:"
                    + cls._normalize_decimal(number)
                )
            else:
                facts.add(
                    "number:"
                    + cls._normalize_decimal(raw_value)
                )

        return facts

    @staticmethod
    def _normalize_decimal(value: str) -> str:
        decimal_value = Decimal(
            value.replace(",", "")
        )

        normalized = format(
            decimal_value.normalize(),
            "f",
        )

        # Decimal("-0") 统一成 "0"
        if decimal_value == 0:
            return "0"

        return normalized

    @staticmethod
    def _normalize_date(value: str) -> str:
        normalized = (
            value.replace("年", "-")
            .replace("月", "-")
            .replace("日", "")
            .replace("/", "-")
        )

        year, month, day = normalized.split("-")

        try:
            # 同时检查 2 月 30 日等非法日期。
            parsed = date(
                int(year),
                int(month),
                int(day),
            )
        except ValueError:
            # 模型生成非法日期时不能让问答接口崩溃。
            # 保留标准化原值，使其只能与证据中的
            # 同一个非法日期匹配。
            return f"invalid-date:{normalized}"

        return f"date:{parsed.isoformat()}"

    @staticmethod
    def _display_fact(fact: str) -> str:
        fact_type, value = fact.split(":", 1)

        if fact_type == "percent":
            return f"{value}%"

        if fact_type == "date":
            return value

        return value
