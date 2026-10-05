"""Request-local, opt-in pipeline snapshots. No global logs or API debug fields."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import PurePosixPath


@dataclass
class EvaluationTrace:
    stages: dict = field(default_factory=dict)
    scope: dict = field(default_factory=dict)
    query: str = ""

    def to_dict(self):
        return deepcopy(dict(query=self.query, scope=self.scope, stages=self.stages))


_current = ContextVar("evaluation_trace", default=None)


@contextmanager
def capture_trace():
    trace = EvaluationTrace()
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


def record_scope(query, scope):
    trace = _current.get()
    if trace is not None:
        trace.query = query
        trace.scope = dict(tenant_id=str(scope.tenant_id),
                           knowledge_base_ids=sorted(map(str, scope.knowledge_base_ids)))


def _in_scope(trace, metadata):
    return (str(metadata.get("tenant_id")) == trace.scope.get("tenant_id")
            and str(metadata.get("knowledge_base_id")) in trace.scope.get("knowledge_base_ids", []))


def record_documents(stage, documents):
    trace = _current.get()
    if trace is None:
        return
    rows = []
    for rank, document in enumerate(documents, 1):
        metadata = document.metadata
        # A faulty retriever must not cause a diagnostic file to leak other scopes.
        if not _in_scope(trace, metadata):
            rows.append(dict(rank=rank, scope_violation=True, content="", document_name=""))
            continue
        name = metadata.get("document_name") or PurePosixPath(str(metadata.get("source", "")).replace("\\", "/")).name
        rows.append(dict(rank=rank, document_name=str(name), content=document.page_content,
                         chunk_id=str(getattr(document, "id", None) or metadata.get("chunk_id") or ""),
                         tenant_id=str(metadata.get("tenant_id")),
                         knowledge_base_id=str(metadata.get("knowledge_base_id")),
                         rerank_status=metadata.get("rerank_status"),
                         policy=metadata.get("policy"),
                         policy_selection=metadata.get("policy_selection"),
                         evidence_role=metadata.get("evidence_role"),
                         quality=metadata.get("quality"),
                         cleaning_version=metadata.get("cleaning_version"),
                         chunk_type=metadata.get("chunk_type"),
                         parser=metadata.get("parser"),
                         section_path=metadata.get("section_path")))
    trace.stages[stage] = rows


def record_context(context):
    trace = _current.get()
    if trace is None:
        return
    rows = []
    for item in context.items:
        if str(item.knowledge_base_id) not in trace.scope.get("knowledge_base_ids", []):
            rows.append(dict(scope_violation=True, content="", document_name=""))
        else:
            rows.append(dict(document_name=item.document_name, content=item.content,
                             chunk_id=item.chunk_id, citation_id=item.citation_id,
                             policy=item.policy,
                             evidence_role=item.evidence_role,
                             quality=item.quality,
                             parser=item.parser,
                             cleaning_version=item.cleaning_version,
                             knowledge_base_id=str(item.knowledge_base_id)))
    trace.stages["context"] = rows


def record_calculations(records):
    trace = _current.get()
    if trace is not None:
        trace.stages["calculations"] = deepcopy(records)
