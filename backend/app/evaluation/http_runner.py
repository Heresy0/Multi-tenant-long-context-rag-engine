"""Real HTTP multi-turn and ACL checks using pre-provisioned identities only."""
import json
import time
from urllib.parse import urlparse
from uuid import UUID

import requests

from .metrics import normalize, score_answer, summarize_records
from .runner import token_from_environment


class EvaluationHttpClient:
    def __init__(self, config, *, client=None):
        self.config = config
        self.base_url = config["base_url"].rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid evaluation base_url")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Remote evaluation endpoints must use HTTPS")
        self.client = client or requests.Session()
        self.timeout = float(config.get("timeout_seconds", 120))
        self.pause = float(config.get("delay_seconds", 1))
        if self.timeout <= 0 or self.pause < 0:
            raise ValueError("Invalid HTTP timeout/delay")

    def request(self, method, path, profile, **kwargs):
        if not path.startswith("/api/") or ".." in path or urlparse(path).netloc:
            raise ValueError("Fixture paths must be local /api/ paths")
        token = token_from_environment(self.config["profiles"][profile]["token_env"])
        started = time.perf_counter()
        # Do not follow redirects: they can move bearer tokens to another endpoint.
        response = self.client.request(method, self.base_url + path,
                                       headers={"Authorization": f"Bearer {token}"},
                                       timeout=self.timeout, allow_redirects=False, **kwargs)
        latency = round((time.perf_counter() - started) * 1000, 2)
        if self.pause:
            time.sleep(self.pause)
        return response, latency

    def verify_profile(self, profile):
        setting = self.config["profiles"][profile]
        response, _ = self.request("GET", "/api/knowledge-bases", profile)
        response.raise_for_status()
        actual = {str(UUID(item["id"])) for item in response.json()["items"]}
        expected = {str(UUID(self.config["knowledge_bases"][group])) for group in setting["expected_groups"]}
        if actual != expected:
            raise ValueError("Effective readable KBs do not match profile fixture")
        return actual


def evaluate_conversations(sessions, config, *, client=None):
    api = EvaluationHttpClient(config, client=client)
    records = []
    for case in sessions:
        profile = case.get("profile", "全范围测试用户")
        conversation_id = None
        turn_records = []
        record = dict(id=case["id"], status="error", turns=turn_records)
        try:
            api.verify_profile(profile)
            created, _ = api.request("POST", "/api/conversations", profile)
            if created.status_code != 201:
                raise ValueError("Conversation creation did not return 201")
            conversation_id = str(UUID(created.json()["id"]))
            broken = False
            for turn in case["turns"]:
                if broken:
                    turn_records.append(dict(id=turn["id"], status="skipped", reason="previous_turn_error"))
                    continue
                try:
                    response, latency = api.request("POST", "/api/qa", profile, json={
                        "question": turn["question"], "conversation_id": conversation_id})
                    response.raise_for_status()
                    case_for_score = dict(turn, answerable=turn.get("answerable", True), expected_evidence=[])
                    metrics = score_answer(case_for_score, response.json())
                    detail, _ = api.request("GET", f"/api/conversations/{conversation_id}", profile)
                    detail.raise_for_status()
                    latest = detail.json()["turns"][-1]
                    turn_records.append(dict(id=turn["id"], status="passed" if metrics["automatic_proxy_pass"] else "failed",
                                             latency_ms=latency, metrics=metrics, response=response.json(),
                                             retrieval_question=latest["retrieval_question"],
                                             rewrite_correctness="requires_manual_review"))
                except Exception as exc:
                    broken = True
                    turn_records.append(dict(id=turn["id"], status="error", error_type=type(exc).__name__))
            state = "error" if any(row["status"] == "error" for row in turn_records) else "failed" if any(row["status"] == "failed" for row in turn_records) else "passed"
            record = dict(id=case["id"], status=state, turns=turn_records)
        except Exception as exc:
            record = dict(id=case["id"], status="error", error_type=type(exc).__name__, turns=turn_records)
        finally:
            if conversation_id is not None:
                try:
                    deleted, _ = api.request("DELETE", f"/api/conversations/{conversation_id}", profile)
                    record["cleanup"] = "deleted_owned_evaluation_conversation" if deleted.status_code == 204 else "failed"
                    if deleted.status_code != 204:
                        record["status"] = "error"
                except Exception as exc:
                    record["cleanup"] = "failed"
                    record["cleanup_error_type"] = type(exc).__name__
                    record["status"] = "error"
        records.append(record)
        print(f"Evaluated conversation {case['id']}", flush=True)
    return dict(status="completed", evaluation_type="real_http_conversation_api",
                summary=summarize_records(records), records=records,
                limitations=["Creates then deletes only evaluation-owned conversations; append-only audit events remain.",
                             "Answer/fact/source checks are proxies; rewrite correctness needs manual review."])


def evaluate_permissions(permission, config, *, client=None):
    api = EvaluationHttpClient(config, client=client)
    fixtures = config.get("security_fixtures", {})
    records = []
    positive = {"ACL-01": (["650元"], ["公司公共"]),
                "ACL-03": (["5个工作日"], ["人力资源部"]),
                "ACL-05": (["6次"], ["平台研发部"]),
                "ACL-07": (["13分钟", "6次"], ["技术部", "平台研发部"])}
    negative = {"ACL-02": ["7个工作日", "季度绩效评估与复核流程"],
                "ACL-04": ["13分钟", "报表导出重复提交事件复盘"],
                "ACL-06": ["7日", "7天", "Webhook投递重试与幂等处理规范"],
                "ACL-09": ["7日", "7天", "Webhook投递重试与幂等处理规范"]}
    for case in permission["cases"]:
        profile = case["profile"]
        fixture = fixtures.get(case["id"], {})
        if profile not in config["profiles"]:
            records.append(dict(id=case["id"], status="skipped", reason="profile_not_configured"))
            continue
        # Revocation, ownership and tenant fixtures need explicit, independently
        # verified resources. Never revoke grants or invent random UUIDs here.
        if case["id"] in ("ACL-08", "ACL-10", "ACL-11", "ACL-12"):
            records.append(dict(id=case["id"], status="skipped",
                                reason="requires_isolated_stateful_fixture_use_security_unit_regressions"))
            continue
        # A denied-context question is only meaningful when its required private
        # document is present and no equivalent facts are visible to this profile.
        if case["id"] in negative and not fixture.get("no_equivalent_visible_evidence_verified"):
            records.append(dict(id=case["id"], status="skipped", reason="negative_evidence_fixture_not_verified"))
            continue
        try:
            api.verify_profile(profile)
            response, latency = api.request("POST", "/api/qa", profile, json={"question": case["question"]})
            if response.status_code != 200:
                records.append(dict(id=case["id"], status="error", http_status=response.status_code,
                                    reason="transport_or_auth_error_is_not_an_acl_pass"))
                continue
            data = response.json()
            # Validate the QA shape before a refusal can be counted as safe.
            score_answer(dict(answerable=False, required_facts=[], expected_evidence=[]), data)
            kb_ids = {str(citation.get("knowledge_base_id")) for citation in data["citations"]}
            allowed = {config["knowledge_bases"][group] for group in config["profiles"][profile]["expected_groups"]}
            if case["id"] in positive:
                facts, groups = positive[case["id"]]
                passed = (data["answerable"] and all(normalize(fact) in normalize(data["answer"]) for fact in facts)
                          and {config["knowledge_bases"][group] for group in groups}.issubset(kb_ids)
                          and kb_ids.issubset(allowed))
            else:
                text = normalize(json.dumps(data, ensure_ascii=False))
                terms = [*negative[case["id"]], *fixture.get("forbidden_terms", [])]
                passed = not data["answerable"] and not data["citations"] and not any(normalize(term) in text for term in terms)
            records.append(dict(id=case["id"], status="passed" if passed else "failed", latency_ms=latency,
                                response=data, check="refusal_and_selected_leak_terms_not_exhaustive_semantic_detection"))
        except Exception as exc:
            records.append(dict(id=case["id"], status="error", error_type=type(exc).__name__))
    return dict(status="completed", evaluation_type="real_http_preprovisioned_profile_checks",
                summary=summarize_records(records), records=records,
                limitations=["Stateful revocation/ownership/cross-tenant/empty-scope cases are covered by isolated unit tests, not marked deployed-pass.",
                             "Negative fixtures must prove evidence exists but is absent from this profile; term matching needs manual security review."])
