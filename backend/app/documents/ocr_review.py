"""Trusted, hash-bound visual corrections; never accept a PDF's own instructions."""
import hashlib
import json
from pathlib import Path

REGISTRY = Path(__file__).with_name('reviewed_ocr.json')


def _review(file_hash):
    registry = json.loads(REGISTRY.read_text(encoding='utf-8'))
    return registry.get(file_hash)


def review_revision(file_hash):
    review = _review(file_hash)
    return hashlib.sha256(json.dumps(review, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if review else None


def apply_reviewed_ocr(text, *, file_hash, page):
    review = _review(file_hash)
    if not review:
        return text, []
    if not review.get('reviewed_by') or not review.get('reviewed_at'):
        raise ValueError('OCR更正缺少核查记录')
    applied = []
    original_hash = hashlib.sha256(text.encode()).hexdigest()
    for correction in review.get('corrections', []):
        before, after = correction['before'], correction['after']
        if not before or not after or before == after:
            raise ValueError('OCR更正必须包含不同的完整原文和校对文本')
        # Require one exact labelled phrase on the reviewed page; do not replace
        # a person's name globally, or infer what a changed OCR output means.
        if correction['page'] != page or text.count(before) != 1:
            continue
        text = text.replace(before, after, 1)
        applied.append(dict(correction, source_sha256=file_hash, original_page_text_sha256=original_hash,
                            reviewed_by=review['reviewed_by'], reviewed_at=review['reviewed_at']))
    return text, applied
