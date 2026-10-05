"""Authenticated local multi-turn diagnostics; never expose traces through HTTP.

This executes real conversation/retrieval/generation services, not a second
retrieval probe. It is deliberately labelled separately from HTTP evaluation.
"""
import hashlib
import json
import time
from uuid import UUID

from ..config import Settings
from ..db.session import create_database_engine, create_session_factory
from ..knowledge.answer_service import AnswerService
from ..knowledge.conversation_service import ConversationAnswerService, ConversationResolver
from ..knowledge.conversations import ConversationStore
from ..knowledge.retrieval_service import RetrievalService
from ..security.authorization import AuthorizationService
from .metrics import score_answer, score_generation, summarize_records, summarize_generation
from .runner import token_from_environment, verified_principal
from .trace import capture_trace


def evaluate_local_conversations(cases, config, *, save_traces=None):
    settings = Settings()
    engine = create_database_engine(settings.database_url)
    records, stream = [], None
    try:
        if save_traces:
            stream = save_traces.open('x', encoding='utf-8')
        service = None  # Initialize paid clients only after identity/scope checks.
        for case in cases:
            turns, conversation_id, store = [], None, None
            record = dict(id=case['id'], status='error', turns=turns)
            with create_session_factory(engine)() as session:
                try:
                    profile = config['profiles'][case.get('profile', '全范围测试用户')]
                    principal = verified_principal(settings, session, token_from_environment(profile['token_env']))
                    authorization = AuthorizationService(session)
                    expected = {UUID(config['knowledge_bases'][group]) for group in profile['expected_groups']}
                    if authorization.list_readable_knowledge_base_ids(principal=principal) != expected:
                        raise ValueError('Effective readable scope does not match fixture')
                    scope = authorization.require_retrieval_scope(principal=principal, knowledge_base_id=None)
                    store = ConversationStore(session, principal, scope)
                    conversation_id = store.create().id
                    if service is None:
                        service = ConversationAnswerService(
                            AnswerService(settings=settings, retrieval_service=RetrievalService(settings)),
                            ConversationResolver(settings),
                        )
                    broken = False
                    for turn in case['turns']:
                        if broken:
                            turns.append(dict(id=turn['id'], status='skipped', reason='previous_turn_error'))
                            continue
                        trace, snapshot = None, None
                        started = time.perf_counter()
                        try:
                            # Re-verify JWT and current authorization on every turn.
                            principal = verified_principal(settings, session,
                                                           token_from_environment(profile['token_env']))
                            scope = authorization.require_retrieval_scope(principal=principal, knowledge_base_id=None)
                            with capture_trace() as trace:
                                result, _ = service.answer(turn['question'], conversation_id=conversation_id,
                                                           principal=principal, scope=scope, session=session)
                            snapshot = trace.to_dict()
                            response = result.model_dump(mode='json')
                            metrics = score_answer(dict(turn, answerable=turn.get('answerable', True),
                                                        expected_evidence=turn.get('expected_evidence', [])),
                                                   response, snapshot['stages'].get('context'))
                            metrics['generation'] = score_generation(
                                dict(turn, answerable=turn.get('answerable', True)), snapshot)
                            turns.append(dict(id=turn['id'], status='passed' if metrics['automatic_proxy_pass'] else 'failed',
                                              metrics=metrics, response=response, retrieval_question=trace.query,
                                              latency_ms=round((time.perf_counter() - started) * 1000, 2),
                                              rewrite_correctness='requires_manual_review'))
                            snapshot['response'] = response
                        except Exception as exc:
                            broken = True
                            turns.append(dict(id=turn['id'], status='error', error_type=type(exc).__name__))
                            snapshot = trace.to_dict() if trace is not None else dict(stages={}, scope={})
                            snapshot['error_type'] = type(exc).__name__  # No credential-bearing exception strings.
                            session.rollback()
                        if stream is not None:
                            snapshot.update(id=turn['id'], conversation_case_id=case['id'],
                                            question_hash=hashlib.sha256(turn['question'].encode()).hexdigest(),
                                            evaluation_type='authenticated_local_conversation_service_not_http')
                            stream.write(json.dumps(snapshot, ensure_ascii=False) + '\n')
                            stream.flush()
                    record['status'] = ('error' if any(row['status'] == 'error' for row in turns)
                                        else 'failed' if any(row['status'] == 'failed' for row in turns) else 'passed')
                except Exception as exc:
                    record.update(status='error', error_type=type(exc).__name__)
                    session.rollback()
                finally:
                    if conversation_id is not None:
                        try:
                            # Delete only the ID created by this iteration, never existing user sessions.
                            session.rollback()
                            store.delete(conversation_id)
                            record['cleanup'] = 'deleted_owned_evaluation_conversation'
                        except Exception as exc:
                            record.update(status='error', cleanup='failed', cleanup_error_type=type(exc).__name__)
                            session.rollback()
            records.append(record)
            print(f"Evaluated local conversation {case['id']}", flush=True)
        return dict(status='completed', evaluation_type='authenticated_local_conversation_service_not_http',
                    summary=summarize_records(records), records=records,
                    turn_summary=summarize_records([turn for row in records for turn in row['turns']]),
                    generation_metrics=summarize_generation([turn for row in records for turn in row['turns']]),
                    limitations=['Opt-in snapshots contain authorized business text; keep private.',
                                 'Local service evaluation does not test HTTP gateway, audit or rate limiting.',
                                 'Temporary test conversations are created and deleted; no permission changes.',
                                 'Keyword/source/citation checks are proxies, not full causal or semantic validation.'])
    finally:
        if stream is not None:
            stream.close()
        engine.dispose()
