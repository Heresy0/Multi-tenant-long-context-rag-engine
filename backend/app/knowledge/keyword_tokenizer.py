"""Stable v1 tokenizer shared by indexing, querying and historical backfill.

Changing these rules requires a new tokenization version and a full keyword backfill.
"""
import re
import unicodedata

import jieba

KEYWORD_TOKENIZATION_VERSION = "jieba-v1"
SPECIAL_TOKEN_PATTERN = re.compile(
    r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+|\d+(?:\.\d+)?%?",
    flags=re.IGNORECASE,
)


def tokenize_chinese(text: str) -> list[str]:
    """Preserve Chinese search words, numbers, error codes and API identifiers."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens = [
        token.strip()
        for token in jieba.cut_for_search(normalized)
        if token.strip() and re.search(r"[a-z0-9\u4e00-\u9fff]", token)
    ]
    tokens.extend(SPECIAL_TOKEN_PATTERN.findall(normalized))
    return tokens


def build_keyword_text(text: str) -> str:
    # Keep repeated terms: term frequency participates in BM25 scoring.
    return " ".join(tokenize_chinese(text))
