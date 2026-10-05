import hashlib
from pathlib import Path

from backend.app.documents.ocr_review import apply_reviewed_ocr, review_revision
from backend.app.documents.pdf_splitter import split_pdf

SOURCE_HASH = '76c053e7d68534aec868a8721a6b6a88ffd4f5660b272f62ebfddd1049916b53'


def test_review_is_bound_to_source_page_and_exact_label_not_global_name():
    raw = '周末值班正常。验证负责人: 周末。'
    corrected, records = apply_reviewed_ocr(raw, file_hash=SOURCE_HASH, page=1)
    assert corrected == '周末值班正常。验证负责人: 周宁。'
    assert records[0]['original_page_text_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert records[0]['reviewed_by'] == 'codex_visual_check'
    for source_hash, page, text in [('other', 1, raw), (SOURCE_HASH, 2, raw),
                                    (SOURCE_HASH, 1, '负责人: 周末'), (SOURCE_HASH, 1, raw + raw)]:
        assert apply_reviewed_ocr(text, file_hash=source_hash, page=page) == (text, [])
    assert review_revision('other') is None
    assert review_revision(SOURCE_HASH)


def test_correction_precedes_chunking_and_keeps_review_required():
    import pytest
    source = Path('output/pdf/xinghai-department-corpus-v3/documents/04_平台研发部/12_生产维护通知_扫描件.pdf')
    if not source.is_file():
        pytest.skip('Local test corpus unavailable')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    chunks = split_pdf(source, ocr_page=lambda *_: '验证负责人: 周末。\n已有任务保留。')
    assert '周宁' in '\n'.join(chunk.page_content for chunk in chunks)
    corrected = [chunk for chunk in chunks if chunk.metadata.get('ocr_corrections')]
    assert corrected and all(chunk.metadata['quality']['review_required'] for chunk in corrected)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
