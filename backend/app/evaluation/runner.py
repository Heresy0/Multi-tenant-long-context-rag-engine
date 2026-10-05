"""Replay and authenticated local-service quality evaluation.

Local service timing is not HTTP performance; all database access is read-only.
"""
import hashlib
import json
import os
import time
import sys
from pathlib import Path
from uuid import UUID

from .metrics import evidence_metrics, score_answer, summarize_records, summarize_stages
from .trace import capture_trace, record_context


def score_trace(case, trace, *, include_answer=False):
    stages = trace.get("stages", {})
    metrics = {stage: evidence_metrics(case["expected_evidence"], rows,
                                     alternative_sets=case.get("accepted_evidence_sets", []))
               for stage, rows in stages.items() if stage in ("vector", "keyword", "fusion", "rerank", "context")}
    violations = sum(bool(row.get("scope_violation")) for rows in stages.values() for row in rows)
    if trace.get("error_type"):
        return dict(id=case["id"], status="error", error_type=trace["error_type"], metrics=metrics)
    status = "partial"
    if case["answerable"] and "context" in metrics:
        status = "passed" if metrics["context"]["all_evidence_present"] else "failed"
    if include_answer:
        if "response" not in trace:
            return dict(id=case["id"], status="skipped", reason="no_final_response_in_trace", metrics=metrics)
        metrics["answer"] = score_answer(case, trace["response"], stages.get("context"))
        status = "passed" if metrics["answer"]["automatic_proxy_pass"] else "failed"
    if violations:
        status = "failed"
    return dict(id=case["id"], category=case.get("category"), source_groups=case.get("source_groups", []),
                difficulty=case.get("difficulty"), status=status,
                metrics=metrics, scope_violation_count=violations,
                latency_ms=trace.get("latency_ms"), response=trace.get("response"),
                limitation="Automatic proxy results require semantic review; unlabelled negatives have no retrieval score.")


def replay(cases, trace_path, *, include_answer=False):
    traces = {}
    for line in trace_path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["id"] in traces:
            raise ValueError("Duplicate case ID in trace file")
        traces[row["id"]] = row
    records = []
    for case in cases:
        if case["id"] not in traces:
            records.append(dict(id=case["id"], status="skipped", reason="trace_missing"))
            continue
        fingerprint = traces[case["id"]].get("question_hash")
        if fingerprint and fingerprint != hashlib.sha256(case["question"].encode()).hexdigest():
            records.append(dict(id=case["id"], status="skipped", reason="trace_question_changed"))
            continue
        try:
            records.append(score_trace(case, traces[case["id"]], include_answer=include_answer))
        except (KeyError, TypeError, ValueError):
            records.append(dict(id=case["id"], status="error", reason="invalid_trace_record"))
    return dict(status="completed", evaluation_type="replay_not_a_new_live_run",
                summary=summarize_records(records), stages=summarize_stages(records), records=records)


def token_from_environment(name):
    token = os.getenv(name, "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or len(token.split(".")) != 3:
        raise ValueError(f"Missing or invalid JWT in environment variable {name}")
    return token


def verified_principal(settings, session, token):
    from sqlalchemy import select
    from ..db.models import User
    from ..security.oidc import OidcTokenVerifier
    from ..security.principal import Principal
    claims = OidcTokenVerifier(issuer=settings.oidc_issuer, audience=settings.oidc_audience,
                               jwks_url=settings.oidc_jwks_url).verify(token)
    user = session.scalars(select(User).where(User.external_subject == claims.subject)).one_or_none()
    if user is None or user.status != "active":
        raise ValueError("Verified identity is not an active mapped application user")
    return Principal(user_id=user.id, tenant_id=user.tenant_id, external_subject=user.external_subject)


def evaluate_local(cases, config, *, include_answer=False, save_traces=None, include_index=False, expected_documents=()):
    from sqlalchemy import select
    from ..config import Settings
    from ..db.models import DocumentChunk, KnowledgeDocument, EMBEDDING_DIMENSION
    from ..db.session import create_database_engine, create_session_factory
    from ..knowledge.answer_service import AnswerService
    from ..knowledge.context_builder import ContextBuilder
    from ..knowledge.retrieval_service import RetrievalService
    from ..security.authorization import AuthorizationService
    settings = Settings()
    kb_map = {group: UUID(value) for group, value in config["knowledge_bases"].items()}
    profile = config["profiles"]["全范围测试用户"]
    token = token_from_environment(profile["token_env"])
    engine = create_database_engine(settings.database_url)
    records, index_records = [], []
    trace_stream = None
    try:
        if save_traces:
            # Sensitive diagnostic files must never silently replace a prior run.
            trace_stream = save_traces.open("x", encoding="utf-8")
        with create_session_factory(engine)() as session:
            principal = verified_principal(settings, session, token)
            authorization = AuthorizationService(session)
            actual_ids = authorization.list_readable_knowledge_base_ids(principal=principal)
            expected_ids = {kb_map[group] for group in profile["expected_groups"]}
            if actual_ids != expected_ids:
                raise ValueError("Full profile effective scope does not match configured fixture")
            if include_index:
                if not {kb_map[row["group"]] for row in expected_documents}.issubset(actual_ids):
                    raise ValueError("Corpus index audit requires access to every expected corpus group")
                expected = {(kb_map[row["group"]], Path(row["file"]).name): row["sha256"]
                            for row in expected_documents}
                seen = set()
                for doc in session.scalars(select(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == principal.tenant_id,
                        KnowledgeDocument.knowledge_base_id.in_(actual_ids))):
                    chunks = session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)).all()
                    valid = (doc.status == "ready" and bool(chunks)
                             and len(chunks) == doc.metadata_json.get("chunk_count")
                             and all(chunk.tenant_id == doc.tenant_id and chunk.knowledge_base_id == doc.knowledge_base_id
                                     and len(chunk.embedding) == EMBEDDING_DIMENSION and chunk.keyword_text.strip()
                                     for chunk in chunks))
                    key = (doc.knowledge_base_id, doc.file_name)
                    seen.add(key)
                    if key in expected:
                        valid = valid and doc.content_hash == expected[key]
                    index_records.append(dict(id=str(doc.id), file_name=doc.file_name,
                                              status="passed" if valid else "failed", document_status=doc.status,
                                              chunk_count=len(chunks)))
                for key in expected.keys() - seen:
                    index_records.append(dict(id=key[1], status="failed", reason="expected_corpus_document_missing"))
            if cases:
                retrieval = RetrievalService(settings)
                answer = AnswerService(settings=settings, retrieval_service=retrieval) if include_answer else None
                builder = ContextBuilder()
                for case in cases:
                    started = time.perf_counter()
                    trace = None
                    try:
                        kb_id = kb_map.get(case.get("expected_scope"))
                        scope = authorization.require_retrieval_scope(principal=principal, knowledge_base_id=kb_id)
                        required_ids = {kb_map[group] for group in case.get("source_groups", [])}
                        if not required_ids.issubset(scope.knowledge_base_ids):
                            records.append(dict(id=case["id"], status="skipped", reason="required_sources_not_authorized"))
                            continue
                        with capture_trace() as trace:
                            if answer is not None:
                                response = answer.answer(case["question"], scope=scope, session=session).model_dump(mode="json")
                            else:
                                documents = retrieval.search(case["question"], scope=scope, session=session)
                                documents = [doc for doc in documents if scope.contains(
                                    doc.metadata.get("tenant_id"), doc.metadata.get("knowledge_base_id"))]
                                record_context(builder.build(documents, question=case["question"]))
                                response = None
                        snapshot = dict(trace.to_dict(), id=case["id"], schema_version=1,
                                        origin="jwt_verified_local_backend_services",
                                        latency_ms=round((time.perf_counter() - started) * 1000, 2),
                                        question_hash=hashlib.sha256(case["question"].encode()).hexdigest())
                        if response is not None:
                            snapshot["response"] = response
                        if trace_stream:
                            trace_stream.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
                            trace_stream.flush()
                        records.append(score_trace(case, snapshot, include_answer=include_answer))
                    except Exception as exc:
                        session.rollback()
                        if trace_stream and trace is not None:
                            trace_stream.write(json.dumps(dict(trace.to_dict(), id=case["id"], schema_version=1,
                                origin="jwt_verified_local_backend_services", error_type=type(exc).__name__,
                                question_hash=hashlib.sha256(case["question"].encode()).hexdigest()), ensure_ascii=False) + "\n")
                            trace_stream.flush()
                        records.append(dict(id=case["id"], status="error", error_type=type(exc).__name__))
                    print(f"Evaluated {case['id']}", file=sys.stderr, flush=True)
            # No service writes/commits are called; rollback also releases read transactions.
            session.rollback()
    finally:
        if trace_stream:
            trace_stream.close()
        engine.dispose()
    return dict(status="completed", evaluation_type="authenticated_local_backend_not_http_endpoint",
                runtime=dict(tenant_id=str(principal.tenant_id), user_id=str(principal.user_id),
                             embedding_model=settings.embedding_model, rerank_model=settings.rerank_model,
                             answer_model=settings.chat_model),
                summary=summarize_records(records), stages=summarize_stages(records), records=records,
                index=dict(status="completed" if include_index else "skipped",
                           summary=summarize_records(index_records), records=index_records),
                limitations=["Local execution bypasses HTTP rate limiting/audit; use separate HTTP security and Locust tests.",
                             "Index checks compare corpus hashes with DB metadata, not stored-upload bytes or update/delete convergence."])
