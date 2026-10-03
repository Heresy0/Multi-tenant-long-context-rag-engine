"""Offline corpus validation with the project's splitters; never calls an API."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.documents.document_splitter import split_docx
from backend.app.documents.pdf_splitter import PdfOcrUnavailableError, split_pdf
from backend.app.documents.text_splitter import split_markdown, split_txt
from scripts.evaluate_answers import load_dataset


def normalize(value: str) -> str:
    return "".join(value.split())


def validate():
    folder = ROOT / "output/pdf/xinghai-tech-corpus"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    cases = load_dataset(folder / "evaluation/questions.jsonl")
    records = []
    all_chunks = {}
    for doc in manifest["documents"]:
        path = folder / doc["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == doc["sha256"], path
        if "OCR必需" in doc["features"]:
            # Deliberately test missing-OCR handling without substituting ground truth.
            def unavailable(*_):
                raise PdfOcrUnavailableError("Offline validation: no OCR engine available")
            try:
                split_pdf(path, ocr_page=unavailable)
            except PdfOcrUnavailableError:
                records.append({"file": path.name, "result": "expected OCR requirement",
                                "actual_ocr": "not tested", "chunks": None})
                continue
            raise AssertionError("An image-only PDF must require OCR")
        splitter = {"docx": split_docx, "pdf": split_pdf, "md": split_markdown, "txt": split_txt}[doc["format"]]
        chunks = splitter(path)
        assert chunks and all(c.page_content.strip() for c in chunks), path
        all_chunks[path.name] = chunks
        records.append({"file": path.name, "result": "parsed", "chunks": len(chunks),
                        "chunk_types": sorted({c.metadata["chunk_type"] for c in chunks})})
    checks = []
    for case in cases:
        evidence_checks = []
        for evidence in case["expected_evidence"]:
            name = evidence["document"]
            if name not in all_chunks:
                evidence_checks.append({"file": name, "result": "pending actual OCR"})
                continue
            target = normalize(evidence["text"])
            found = any(target in normalize(c.page_content) for c in all_chunks[name])
            evidence_checks.append({"file": name, "result": "present in chunk" if found else "not found as exact substring"})
        checks.append({"id": case["id"], "evidence": evidence_checks})
    cross = all_chunks["09_客户数据外发审批补充规定.pdf"]
    joined = any("书面批准" in c.page_content and "72小时" in c.page_content
                 and c.metadata["page_start"] == 1 and c.metadata["page_end"] == 2 for c in cross)
    columns = all_chunks["11_生产运维值班与回退手册.pdf"]
    column_text = normalize("\n".join(c.page_content for c in columns))
    report = {"type": "offline parsing, not a retrieval/answer score",
              "documents": records, "parsed_documents": len(all_chunks),
              "total_chunks": sum(len(chunks) for chunks in all_chunks.values()),
              "validated_single_turn_cases": len(cases),
              "evidence_checks": checks, "cross_page_condition_in_one_chunk": joined,
              "dual_column_exact_condition_preserved": "核心API成功率连续10分钟低于99.0%" in column_text,
              "external_model_calls": 0}
    (folder / "evaluation/splitter_checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("documents", "evidence_checks")}, ensure_ascii=False))
    misses = [{"id": row["id"], "evidence": e} for row in checks for e in row["evidence"]
              if e["result"] == "not found as exact substring"]
    print(json.dumps({"exact_evidence_misses": misses}, ensure_ascii=False))


if __name__ == "__main__":
    validate()
