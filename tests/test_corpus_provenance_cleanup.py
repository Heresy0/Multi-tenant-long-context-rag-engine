"""Local corpus regression; no OCR/embedding/LLM calls."""
import hashlib
import csv
import json
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'output/pdf/xinghai-department-corpus-v3'


def test_business_corpus_keeps_provenance_outside_bodies_and_preserves_versions():
    if not (CORPUS / 'manifest.json').exists():
        pytest.skip('Local generated corpus unavailable')
    manifest = json.loads((CORPUS / 'manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('body_provenance_policy'):
        pytest.skip('Original corpus has not been cleaned')
    assert manifest['fictional'] is True and manifest['total_documents'] == 30
    assert '合成设定' in (CORPUS / 'README.md').read_text(encoding='utf-8')
    by_file = {row['file']: row for row in manifest['documents']}
    with (CORPUS / '入库清单.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 30
    assert all(row['SHA256'] == by_file[row['相对路径']]['sha256'] for row in rows)
    bodies = {}
    for row in manifest['documents']:
        path = CORPUS / row['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']
        assert path.stat().st_size == row['bytes']
        if path.suffix == '.pdf':
            text = '\n'.join(p.extract_text() or '' for p in PdfReader(path).pages)
        elif path.suffix == '.docx':
            text = '\n'.join(p.text for p in Document(path).paragraphs)
        else:
            text = path.read_text(encoding='utf-8')
        assert not any(w in text for w in ('虚构测试资料', '企业 RAG 测试语料', '文件说明：模拟企业文件', '评测语料只保留'))
        bodies[path.name[:2]] = text
    assert all(w in bodies['08'] for w in ('550', '已归档', '不适用于当前报销', '2025年1月1日', '2026年6月30日'))
    assert all(w in bodies['27'] for w in ('13分钟', '53分钟', '68分钟', '未发现', '已核查范围'))
    assert '以订单为准' in bodies['06']
    assert '只能使用模拟或脱敏数据' in bodies['15']
    assert '示例中的地址、密钥和业务数据均为虚构' in bodies['15']
    truth = json.loads((CORPUS / 'evaluation/scan_ground_truth.json').read_text(encoding='utf-8'))
    assert '虚构测试资料' not in truth['text'] and '验证负责人：周宁' in truth['text']
