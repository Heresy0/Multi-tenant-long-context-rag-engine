from pathlib import Path

from scripts.evaluate_answers import load_dataset


def test_additional_questions_have_complete_labels_and_are_not_original_case_ids():
    root = Path(__file__).resolve().parents[1]
    cases = load_dataset(root / 'evals/first_pass_generalization/single_turn.jsonl')
    original = load_dataset(root / 'evals/xinghai_v3/single_turn.jsonl')
    assert len(cases) == 8
    assert not {case['id'] for case in cases}.intersection(case['id'] for case in original)
    assert not {case['question'] for case in cases}.intersection(case['question'] for case in original)
    assert all(case['manual_review_required'] and case['semantic_checks'] for case in cases)
    assert {case['expected_scope'] for case in cases} == {'人力资源部', '技术部', '平台研发部'}
    corpus = root / 'output/pdf/xinghai-department-corpus-v3/documents'
    for case in cases:
        for evidence in case['expected_evidence']:
            path, = corpus.rglob(evidence['document'])
            assert evidence['text'] in path.read_text(encoding='utf-8')
