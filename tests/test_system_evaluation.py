import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.documents import Document

from backend.app.evaluation.metrics import evidence_metrics, score_answer, summarize_records
from backend.app.evaluation.trace import capture_trace, record_scope, record_documents, record_context
from backend.app.evaluation.runner import replay, score_trace
from backend.app.evaluation.reporting import compare_baseline, outcome, quality_layers
from backend.app.evaluation.http_runner import EvaluationHttpClient, evaluate_conversations, evaluate_permissions
from backend.app.evaluation.offline import read_performance_report
from backend.app.security.retrieval_scope import RetrievalScope
from scripts.evaluate_system import main, parse_args


def case():
    return dict(id="q1", question="标准？", answerable=True, required_facts=["650元"],
                expected_evidence=[dict(document="制度.docx", text="住宿650元")])


def rows():
    return [dict(document_name="制度", content="住宿650元", chunk_id="c1", citation_id="资料1", knowledge_base_id="kb")]


def response():
    return dict(answer="住宿650元", answerable=True,
                citations=[dict(document_name="制度.docx", chunk_id="c1", citation_id="资料1", knowledge_base_id="kb")])


def test_evidence_requires_both_source_and_text_and_scores_rank():
    metric = evidence_metrics(case()["expected_evidence"], [dict(document_name="其他.docx", content="住宿650元"), *rows()])
    assert metric["reciprocal_rank"] == .5
    assert metric["evidence_recall"] == 1
    assert evidence_metrics(case()["expected_evidence"], [dict(document_name="制度.docx", content="无关段落")])["hit"] is False
    assert evidence_metrics([], rows())["scored"] is False


def test_explicit_fact_aliases_and_numeric_boundaries_do_not_remove_fact_requirements():
    q = dict(case(), required_facts=['不同', '2次'], fact_alternatives={'不同': ['不相同']})
    assert score_answer(q, dict(response(), answer='不相同，2次'))['automatic_proxy_pass']
    assert score_answer(q, dict(response(), answer='不相同，12次'))['missing_facts'] == ['2次']
    assert '不同' in score_answer(q, dict(response(), answer='相同，2次'))['missing_facts']
    assert '不同' in score_answer(dict(q, fact_alternatives={}), dict(response(), answer='不相同，2次'))['missing_facts']


def test_approved_sources_still_require_complete_sets_and_cited_evidence():
    q = dict(case(), accepted_source_document_sets=[['正式手册.docx', '附件.txt']],
             accepted_evidence_sets=[[dict(document='正式手册.docx', text='住宿650元'), dict(document='附件.txt', text='现行')]])
    evidence = [dict(rows()[0], document_name='正式手册.docx'),
                dict(rows()[0], citation_id='资料2', document_name='附件.txt', content='现行')]
    r = dict(response(), citations=[{key: row[key] for key in ('document_name', 'chunk_id', 'citation_id', 'knowledge_base_id')} for row in evidence])
    assert score_answer(q, r, evidence)['automatic_proxy_pass']
    assert not score_answer(q, dict(r, citations=r['citations'][:1]), evidence)['automatic_proxy_pass']
    assert not score_answer(q, r, [dict(row, content='无关') for row in evidence])['automatic_proxy_pass']
    assert not score_answer(dict(q, accepted_source_document_sets=[['正式手册.docx', '其他.txt'], ['别的.docx', '附件.txt']]), r)['automatic_proxy_pass']


def test_multiple_evidence_and_citations_require_all_sources():
    q = case()
    q["expected_evidence"].append(dict(document="附件.pdf", text="现行版本"))
    metric = evidence_metrics(q["expected_evidence"], rows())
    assert metric["hit"] and metric["evidence_recall"] == .5 and not metric["all_evidence_present"]
    result = score_answer(q, response(), rows())
    assert result["all_required_sources_cited"] is False
    assert result["automatic_proxy_pass"] is False


def test_correct_name_but_wrong_chunk_citation_fails():
    r = response()
    r["citations"][0]["chunk_id"] = "invented"
    result = score_answer(case(), r, rows())
    assert result["citation_integrity"] is False
    assert result["cited_evidence_recall"] == 0
    assert result["automatic_proxy_pass"] is False


def test_refusal_and_semantic_metrics_are_not_fake_passes():
    result = score_answer(dict(answerable=False, required_facts=[], expected_evidence=[]),
                          dict(answer="资料不足", answerable=False, citations=[]))
    assert result["automatic_proxy_pass"]
    assert result["fact_coverage"] is None
    assert result["faithfulness"] == "requires_manual_review"
    assert summarize_records([dict(status="skipped")])["pass_rate"] is None


def test_trace_is_opt_in_nested_and_scope_safe():
    scope = RetrievalScope(uuid4(), uuid4())
    doc = Document(page_content="authorized", metadata=dict(tenant_id=str(scope.tenant_id),
                       knowledge_base_id=str(scope.knowledge_base_id), document_name="ok.docx"))
    other = Document(page_content="SECRET", metadata=dict(tenant_id=str(uuid4()), knowledge_base_id=str(uuid4()), document_name="SECRET_TITLE"))
    record_documents("vector", [doc])  # inert outside opt-in context
    with capture_trace() as outer:
        record_scope("q", scope)
        record_documents("vector", [doc, other])
        with capture_trace() as inner:
            assert inner.stages == {}
        assert "vector" in outer.stages
        snapshot = outer.to_dict()
        assert "SECRET" not in json.dumps(snapshot)
        assert snapshot["stages"]["vector"][1]["scope_violation"]
        snapshot["stages"].clear()
        assert outer.stages  # output is a defensive copy
    with capture_trace() as trace:
        assert not trace.stages and not trace.scope


def test_trace_thread_isolation():
    def collect(value):
        scope = RetrievalScope(uuid4(), uuid4())
        with capture_trace() as trace:
            record_scope(value, scope)
            return trace.query
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(collect, ["first", "second"])) == ["first", "second"]


def test_replay_missing_observations_are_not_passed(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(dict(id="q1", stages={"fusion": rows()})) + "\n", encoding="utf-8")
    result = replay([case(), dict(case(), id="q2")], path, include_answer=True)
    assert result["summary"]["skipped"] == 2
    assert result["stages"]["fusion"]["hit_rate"] == 1  # observed retrieval can be scored independently
    assert result["stages"]["context"]["scored_cases"] == 0
    layers = quality_layers(result)
    assert layers["answer"]["summary"]["passed"] == 0


def test_replay_question_version_mismatch_and_scope_violation(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(dict(id="q1", question_hash=hashlib.sha256(b"old question").hexdigest(), stages={})) + "\n")
    assert replay([case()], path)["records"][0]["reason"] == "trace_question_changed"
    trace = dict(stages={"context": rows(), "vector": [dict(scope_violation=True)]}, response=response())
    assert score_trace(case(), trace, include_answer=True)["status"] == "failed"


def test_negative_retrieval_is_not_applicable_not_untested():
    q = dict(case(), answerable=False, required_facts=[], expected_evidence=[])
    quality = dict(records=[score_trace(q, dict(stages={"fusion": [], "rerank": [], "context": []}))], stages={})
    layer = quality_layers(quality)["retrieval"]
    assert layer["summary"]["not_applicable"] == 1
    assert layer["summary"]["skipped"] == 0
    assert layer["summary"]["pass_rate"] is None


def test_incomplete_or_failed_runs_and_legacy_performance_do_not_pass(tmp_path):
    assert outcome(dict(mode="offline", layers={"answer": dict(status="skipped")})) == "incomplete"
    assert outcome(dict(mode="live", layers={"answer": dict(status="completed", summary={"failed": 1})})) == "failed"
    assert outcome(dict(mode="replay", layers={"answer": dict(status="completed")})) == "incomplete"
    path = tmp_path / "old.json"
    path.write_text(json.dumps(dict(passed=True, summary={"request_count": 2, "failure_count": 2}, checks={})))
    assert read_performance_report(path)["status"] == "partial"


def test_cli_defaults_safe_and_requires_explicit_live_calls():
    args = parse_args([])
    assert args.mode == "offline" and not args.allow_model_calls and not args.allow_conversation_writes
    with pytest.raises(SystemExit):
        parse_args(["--mode", "live", "--layers", "retrieval"])
    with pytest.raises(SystemExit):
        parse_args(["--mode", "replay"])
    with pytest.raises(SystemExit):
        parse_args(["--limit", "0"])
    assert parse_args(["--mode", "live", "--layers", "index"]).layers == ["index"]


def test_targeted_ids_are_offline_by_default_and_filter_before_limit(tmp_path):
    args = parse_args(['--case-ids', 'XH-016', 'XH-E-060', '--conversation-ids', 'XH-MT-02'])
    assert args.case_ids == ['XH-016', 'XH-E-060'] and args.mode == 'offline'
    folder = tmp_path / 'report'
    assert main(['--mode', 'offline', '--layers', 'answer', '--case-ids', 'XH-016', 'XH-E-060',
                 '--limit', '1', '--output-dir', str(folder)]) == 0
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    assert report['selected_case_ids'] == ['XH-016']
    assert report['available_cases'] == 60 and report['selected_cases'] == 1
    assert report['live_model_calls_enabled'] is False
    with pytest.raises(ValueError, match='Unknown single-turn'):
        main(['--layers', 'answer', '--case-ids', 'wrong-id', '--output-dir', str(tmp_path / 'bad')])


def config():
    return dict(base_url="http://127.0.0.1:8000", delay_seconds=0,
                knowledge_bases={"公司公共": "00000000-0000-0000-0000-000000000001"},
                profiles={"仅公共测试用户": dict(token_env="TEST_EVAL_TOKEN", expected_groups=["公司公共"])})


class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self.data = status, data

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError("HTTP failure")


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def test_http_never_follows_redirects_or_accepts_scope_mismatch(monkeypatch):
    monkeypatch.setenv("TEST_EVAL_TOKEN", "a.b.c")
    http = FakeHttp([FakeResponse(200, dict(items=[]))])
    api = EvaluationHttpClient(config(), client=http)
    with pytest.raises(ValueError):
        api.verify_profile("仅公共测试用户")
    assert http.calls[0][2]["allow_redirects"] is False
    with pytest.raises(ValueError):
        EvaluationHttpClient(dict(config(), base_url="http://external.example"))


def test_permission_auth_errors_and_missing_fixture_are_not_passes(monkeypatch):
    monkeypatch.setenv("TEST_EVAL_TOKEN", "a.b.c")
    permission = dict(cases=[dict(id="ACL-01", profile="仅公共测试用户", question="标准？"),
                             dict(id="ACL-02", profile="仅公共测试用户", question="HR内容？")])
    http = FakeHttp([FakeResponse(200, dict(items=[dict(id=config()["knowledge_bases"]["公司公共"])])),
                     FakeResponse(401, {})])
    result = evaluate_permissions(permission, config(), client=http)
    assert [row["status"] for row in result["records"]] == ["error", "skipped"]
    assert result["summary"]["passed"] == 0


def test_conversations_cleanup_only_created_owned_id_even_on_turn_failure(monkeypatch):
    monkeypatch.setenv("TEST_EVAL_TOKEN", "a.b.c")
    created_id = str(uuid4())
    http = FakeHttp([FakeResponse(200, dict(items=[dict(id=config()["knowledge_bases"]["公司公共"])])),
                     FakeResponse(201, dict(id=created_id)), FakeResponse(503, {}), FakeResponse(204, {})])
    sessions = [dict(id="mt1", profile="仅公共测试用户", turns=[dict(id="mt1-t1", question="q", required_facts=["fact"]),
                                                                             dict(id="mt1-t2", question="followup", required_facts=["fact"])])]
    result = evaluate_conversations(sessions, config(), client=http)
    assert result["records"][0]["status"] == "error"
    assert result["records"][0]["turns"][1]["status"] == "skipped"
    assert http.calls[-1][0] == "DELETE" and http.calls[-1][1].endswith(created_id)


def test_baseline_rejects_different_scopes_and_compares_like_for_like():
    previous = dict(schema_version=1, mode="offline", dataset_sha256="same", selected_case_ids=["q1"],
                    layers={"context": dict(status="completed", metrics={"mrr": .5}, summary={"pass_rate": .5})})
    current = dict(previous, layers={"context": dict(status="completed", metrics={"mrr": 1}, summary={"pass_rate": 1})})
    result = compare_baseline(current, previous)
    assert result["status"] == "compared" and result["changes"][0]["delta"] == .5
    current["scope_config_sha256"] = "changed scope"
    assert compare_baseline(current, previous)["status"] == "incompatible"


def test_replay_cli_produces_separate_layers_without_network(tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    q = dict(case(), reference_answer="住宿650元")
    (dataset / "single_turn.jsonl").write_text(json.dumps(q, ensure_ascii=False) + "\n", encoding="utf-8")
    trace = tmp_path / "traces.jsonl"
    trace.write_text(json.dumps(dict(id="q1", stages={stage: rows() for stage in
                        ("vector", "keyword", "fusion", "rerank", "context")}, response=response()), ensure_ascii=False) + "\n", encoding="utf-8")
    output = tmp_path / "output"
    code = main(["--mode", "replay", "--layers", "retrieval,rerank,context,answer",
                 "--dataset-dir", str(dataset), "--corpus", str(tmp_path / "no_corpus"),
                 "--trace-input", str(trace), "--output-dir", str(output)])
    assert code == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert set(report["layers"]) == {"retrieval", "rerank", "context", "answer"}
    assert report["overall_status"] == "incomplete"
    assert report["layers"]["answer"]["summary"]["passed"] == 1
    assert report["layers"]["context"]["metrics"]["mean_evidence_recall"] == 1
    assert not report["live_model_calls_enabled"]


def test_answer_trace_uses_actual_trimmed_context_not_raw_candidates(monkeypatch):
    from backend.app.knowledge import answer_service as module
    from backend.app.knowledge.answer_models import AnswerDraft
    from backend.app.knowledge.context_builder import ContextBuilder
    scope = RetrievalScope(uuid4(), uuid4())
    doc = Document(id="c1", page_content="开头。" * 100 + "不可进入截断上下文的尾部",
                   metadata=dict(tenant_id=str(scope.tenant_id), knowledge_base_id=str(scope.knowledge_base_id),
                                 source="private.docx", document_name="private", section_path="说明"))
    retrieval = SimpleNamespace(search=lambda *a, **kw: [doc])
    fake_model = SimpleNamespace(with_structured_output=lambda schema:
                      SimpleNamespace(invoke=lambda messages: AnswerDraft(answerable=False, refusal_reason="资料不足")))
    monkeypatch.setattr(module, "ChatOpenAI", lambda **kwargs: fake_model)
    settings = SimpleNamespace(chat_model="fake", chat_base_url="https://unused.example", chat_api_key="test-only")
    service = module.AnswerService(settings=settings, retrieval_service=retrieval,
                                   context_builder=ContextBuilder(max_characters=100))
    with capture_trace() as trace:
        result = service.answer("q", scope=scope, session=object())
    assert not result.answerable
    assert "不可进入截断上下文的尾部" in trace.stages["authorized_final"][0]["content"]
    assert "不可进入截断上下文的尾部" not in trace.stages["context"][0]["content"]
