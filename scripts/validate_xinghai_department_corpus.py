"""Validate hashes and project splitters offline, optionally exercising real OCR."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.documents.pdf_splitter import split_pdf
from backend.app.documents.document_splitter import split_docx
from backend.app.documents.text_splitter import split_markdown, split_txt


def normalize(value):
    return "".join(value.split()).replace("|", "")


def validate(folder, ocr_only=False):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["total_documents"] == 30
    actual_files = [path for path in (folder / "documents").rglob("*") if path.is_file()]
    assert len(actual_files) == 30
    assert {path.relative_to(folder).as_posix() for path in actual_files} == {row["file"] for row in manifest["documents"]}
    assert dict(Counter(path.suffix[1:] for path in actual_files)) == manifest["counts"]
    chunks_by_name, records = {}, []
    for row in manifest["documents"]:
        path = folder / row["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], path
        scan = "OCR必需" in row["features"]
        if scan != ocr_only:
            continue
        splitter = {"docx": split_docx, "pdf": split_pdf, "md": split_markdown, "txt": split_txt}[row["format"]]
        chunks = splitter(path)
        assert chunks and all(chunk.page_content.strip() for chunk in chunks), path
        chunks_by_name[path.name] = chunks
        records.append(dict(file=row["file"], group=row["group"], chunks=len(chunks),
                            chunk_types=sorted({chunk.metadata["chunk_type"] for chunk in chunks})))
    if ocr_only:
        truth = json.loads((folder / "evaluation/scan_ground_truth.json").read_text(encoding="utf-8"))
        text = normalize("\n".join(chunk.page_content for chunk in chunks_by_name[truth["document"]]))
        anchors = ["XH-MNT-2026-017", "02:00", "03:00", "报表导出", "林澈", "周宁"]
        checks = {anchor: normalize(anchor) in text for anchor in anchors}
        assert all(checks.values()), checks
        result = dict(real_ocr="passed", anchor_checks=checks, documents=records,
                      extracted_text=text, external_model_calls=0)
    else:
        cases = [json.loads(line) for line in (folder / "evaluation/questions.jsonl").read_text(encoding="utf-8").splitlines() if line]
        misses, evidence = [], []
        for case in cases:
            for item in case["expected_evidence"]:
                if item["document"] not in chunks_by_name:
                    evidence.append(dict(id=case["id"], document=item["document"], result="requires separate real OCR check"))
                    continue
                found = any(normalize(item["text"]) in normalize(chunk.page_content)
                            for chunk in chunks_by_name[item["document"]])
                check = dict(id=case["id"], document=item["document"], result="present in chunk" if found else "not found", text=item["text"])
                evidence.append(check)
                if not found:
                    misses.append(check)
        cross = chunks_by_name["09_客户数据外发审批补充规定.pdf"]
        joined = any("书面批准" in chunk.page_content and "72小时" in chunk.page_content
                     and chunk.metadata["page_start"] == 1 and chunk.metadata["page_end"] == 2 for chunk in cross)
        columns = normalize("\n".join(chunk.page_content for chunk in chunks_by_name["11_生产运维值班与回退手册.pdf"]))
        result = dict(kind="offline parsing, not retrieval or model quality evaluation", hashes="passed",
                      parsed_documents=len(records), chunks=sum(row["chunks"] for row in records),
                      single_turn_cases=len(cases), evidence_checks=evidence, exact_evidence_misses=misses,
                      cross_page_condition_preserved=joined,
                      dual_column_condition_preserved="核心API成功率连续10分钟低于99.0%" in columns,
                      documents=records, external_model_calls=0)
        (folder / "evaluation/splitter_checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert joined and result["dual_column_condition_preserved"], result
        assert not misses, misses
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=ROOT / "output/pdf/xinghai-department-corpus-v2")
    parser.add_argument("--ocr-only", action="store_true")
    args = parser.parse_args()
    result = validate(args.folder.resolve(), args.ocr_only)
    if not args.ocr_only:
        result = {key: value for key, value in result.items() if key not in ("documents", "evidence_checks")}
    print(json.dumps(result))
