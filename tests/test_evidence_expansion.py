from types import SimpleNamespace
from uuid import uuid4

from langchain_core.documents import Document

from backend.app.knowledge.evidence_expansion import expand_incident_candidates
from backend.app.security.retrieval_scope import RetrievalScope


def test_identifier_companions_keep_budget_deduplicate_and_reject_out_of_scope():
    scope = RetrievalScope(tenant_id=uuid4(), knowledge_base_id=uuid4())
    doc_id = uuid4()
    metadata = dict(document_id=str(doc_id), tenant_id=str(scope.tenant_id), knowledge_base_id=str(scope.knowledge_base_id), chunk_id='header')
    anchor = Document(page_content='事件编号：INC-2026-1002', metadata=metadata)
    other = Document(page_content='其他候选', metadata=dict(metadata, document_id=str(uuid4()), chunk_id='other'))
    content = SimpleNamespace(content='首次响应耗时为13分钟', metadata=dict(metadata, chunk_id='body'),
                              chunk_id='body', tenant_id=scope.tenant_id, knowledge_base_id=scope.knowledge_base_id)
    intruder = SimpleNamespace(content='SECRET', metadata=dict(metadata, chunk_id='secret'), chunk_id='secret',
                               tenant_id=uuid4(), knowledge_base_id=scope.knowledge_base_id)
    calls = []
    def read(**kwargs):
        calls.append(kwargs)
        return [content, content, intruder]
    result = expand_incident_candidates('INC-2026-1002首次响应耗时？', [anchor, other],
                                       repository=SimpleNamespace(candidate_companions=read), scope=scope, budget=2)
    assert [doc.metadata['chunk_id'] for doc in result] == ['header', 'body']
    assert calls[0]['document_ids'] == [doc_id] and calls[0]['scope'] == scope
    assert 'SECRET' not in str(result)


def test_no_database_expansion_for_other_queries_or_unauthorized_anchor():
    scope = RetrievalScope(tenant_id=uuid4(), knowledge_base_id=uuid4())
    repository = SimpleNamespace()  # Calling any repository method would fail.
    assert expand_incident_candidates('Webhook最多几次？', [], repository=repository, scope=scope) == []
    bad = Document(page_content='INC-2026-1002', metadata=dict(document_id=str(uuid4()), tenant_id=str(uuid4()), knowledge_base_id=str(scope.knowledge_base_id)))
    assert expand_incident_candidates('INC-2026-1002首次响应？', [bad], repository=repository, scope=scope) == [bad]
    authorized = Document(page_content='INC-2026-10020', metadata=dict(bad.metadata, tenant_id=str(scope.tenant_id)))
    assert expand_incident_candidates('INC-2026-1002首次响应？', [authorized], repository=repository, scope=scope) == [authorized]


def test_matching_more_than_two_documents_never_loads_all_of_them():
    scope = RetrievalScope(tenant_id=uuid4(), knowledge_base_id=uuid4())
    docs = [Document(page_content='INC-2026-1002', metadata=dict(document_id=str(uuid4()), tenant_id=str(scope.tenant_id), knowledge_base_id=str(scope.knowledge_base_id))) for _ in range(10)]
    calls = []
    def read(**kwargs):
        calls.append(kwargs)
        return []
    expand_incident_candidates('INC-2026-1002', docs, repository=SimpleNamespace(candidate_companions=read), scope=scope)
    assert len(calls[0]['document_ids']) == 2
