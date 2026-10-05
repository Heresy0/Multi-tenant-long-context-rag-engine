"""Read-only context selection check over old authorized snapshots, not live eval.

Does not regenerate or score answers. Old traces without logical source IDs are
grouped by authorized KB + document name + policy label; this approximation is
reported explicitly. Summary output contains IDs/metrics, not source bodies.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.documents import Document
from backend.app.documents.policy_metadata import policy_label
from backend.app.evaluation.metrics import evidence_metrics
from backend.app.knowledge.context_builder import ContextBuilder
from scripts.evaluate_answers import load_dataset


def check(trace_path, dataset):
    cases = {case['id']: case for case in load_dataset(dataset)}
    records, seen = [], set()
    for line in trace_path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip():
            continue
        trace = json.loads(line)
        identifier = trace.get('id')
        if identifier in seen:
            raise ValueError('Duplicate trace ID')
        seen.add(identifier)
        case = cases.get(identifier)
        if not case or not case['answerable'] or trace.get('error_type'):
            continue
        if trace.get('question_hash') != hashlib.sha256(case['question'].encode('utf-8')).hexdigest():
            raise ValueError('Trace question changed; cannot replay it as a new question')
        stages, scope = trace.get('stages', {}), trace.get('scope', {})
        authorized = stages.get('authorized_final', stages.get('rerank'))
        old_context = stages.get('context')
        if authorized is None or old_context is None:
            continue
        documents = []
        for row in authorized:
            if (row.get('scope_violation') or str(row.get('tenant_id')) != scope.get('tenant_id')
                    or str(row.get('knowledge_base_id')) not in scope.get('knowledge_base_ids', [])):
                raise ValueError('Trace contains out-of-scope candidate')
            source = row.get('source_id') or row.get('document_id') or '\x1f'.join((str(row['knowledge_base_id']),
                       row['document_name'], policy_label(row.get('policy'))))
            documents.append(Document(id=row.get('chunk_id'), page_content=row['content'],
                                      metadata={**row, 'source_id': source}))
        new = ContextBuilder().build(documents, question=case['question'])
        rows = [dict(document_name=item.document_name, content=item.content) for item in new.items]
        old_metrics = evidence_metrics(case['expected_evidence'], old_context,
                                       alternative_sets=case.get('accepted_evidence_sets', []))
        new_metrics = evidence_metrics(case['expected_evidence'], rows,
                                       alternative_sets=case.get('accepted_evidence_sets', []))
        records.append(dict(id=identifier, old_complete=old_metrics['all_evidence_present'],
                            new_complete=new_metrics['all_evidence_present'],
                            old_recall=old_metrics['evidence_recall'], new_recall=new_metrics['evidence_recall'],
                            new_context_characters=new.total_characters))
    if not records:
        raise ValueError('No valid answerable traces; this is not a passing replay')
    return dict(evaluation_type='old_context_reconstruction_not_new_live_answers', scored=len(records),
                old_complete=sum(row['old_complete'] for row in records),
                new_complete=sum(row['new_complete'] for row in records),
                improved=[row['id'] for row in records if not row['old_complete'] and row['new_complete']],
                regressed=[row['id'] for row in records if row['old_complete'] and not row['new_complete']],
                limitation='Old source identity approximated by KB/document/policy when absent; no model calls or DB writes.',
                records=records)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-input', required=True, type=Path)
    parser.add_argument('--dataset', type=Path, default=ROOT / 'evals/xinghai_v3/single_turn.jsonl')
    args = parser.parse_args()
    result = check(args.trace_input, args.dataset)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(1 if result['regressed'] else 0)
