from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
    File,
    Request,
    Response,
    UploadFile,
    )
from sqlalchemy import select
from sqlalchemy.orm import Session
from uuid import UUID, uuid4
from datetime import datetime

from ..db.dependencies import get_database_session
from ..db.models import (
    DocumentIndexingJob,
    KnowledgeBase,
    KnowledgeDocument,
)
from ..schemas import (
    AuditEventListResponse,
    AuditEventSummary,
    KnowledgeBaseListResponse,
    KnowledgeBaseSummary,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentSummary,
    KnowledgeDocumentUploadResponse,
    DocumentIndexingJobDetail,
    DocumentIndexingJobListResponse,
    DocumentIndexingJobSummary,
)
from ..audit_service import AuditService
from ..security.authorization import (
    AuthorizationService,
    AuthorizationDenied,
)
from ..security.dependencies import (
    get_current_principal,
)
from ..security.principal import Principal

from ..documents.document_upload_service import (
    DocumentUploadConflict,
    DocumentUploadService,
    InvalidUpload,
    UploadTooLarge,
)
from ..governance.tenant_document_quota_service import (
    TenantDocumentQuotaExceeded,
)
from ..indexing.document_indexing_job_service import (
    ActiveDocumentIndexingJobExists,
    DocumentIndexingJobConflict,
    DocumentIndexingJobNotFound,
    DocumentIndexingJobService,
    InvalidJobTransition,
)
from ..documents.managed_document_files import (
    ManagedDocumentFileService,
    ManagedDocumentPathError,
)
from ..security.retrieval_scope import RetrievalScope

from ..documents.document_deletion_service import (
    DocumentDeletionService,
    DocumentNotFoundError,
)


router = APIRouter(
    prefix="/api/knowledge-bases",
    tags=["knowledge-bases"],
)


def _indexing_job_detail(
    job: DocumentIndexingJob,
) -> DocumentIndexingJobDetail:
    return DocumentIndexingJobDetail(
        id=job.id,
        document_id=job.document_id,
        status=job.status,
        target_version=job.target_version,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        indexed_chunk_count=job.indexed_chunk_count,
        last_error=job.last_error,
        available_at=job.available_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get(
    "",
    response_model=KnowledgeBaseListResponse,
)
def list_accessible_knowledge_bases(
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> KnowledgeBaseListResponse:
    """列出当前用户有权查询的知识库。"""
    authorization = AuthorizationService(session)

    readable_ids = (
        authorization.list_readable_knowledge_base_ids(
            principal=principal
        )
    )

    if not readable_ids:
        return KnowledgeBaseListResponse(items=[])

    knowledge_bases = session.scalars(
        select(KnowledgeBase)
        .where(
            KnowledgeBase.tenant_id
            == principal.tenant_id,
            KnowledgeBase.id.in_(readable_ids),
            KnowledgeBase.status == "active",
        )
        .order_by(KnowledgeBase.name)
    ).all()

    items: list[KnowledgeBaseSummary] = []

    for knowledge_base in knowledge_bases:
        permission = authorization.get_permission(
            principal=principal,
            knowledge_base_id=knowledge_base.id,
        )

        if permission is None:
            continue

        items.append(
            KnowledgeBaseSummary(
                id=knowledge_base.id,
                name=knowledge_base.name,
                visibility=knowledge_base.visibility,
                permission=permission,
            )
        )

    return KnowledgeBaseListResponse(items=items)


def _document_chunk_count(
    document: KnowledgeDocument,
) -> int:
    value = document.metadata_json.get(
        "chunk_count",
        0,
    )

    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    ):
        return value

    return 0


@router.get(
    "/{knowledge_base_id}/documents",
    response_model=KnowledgeDocumentListResponse,
)
def list_knowledge_base_documents(
    knowledge_base_id: UUID,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> KnowledgeDocumentListResponse:
    """列出当前用户有权查看的知识库文档。"""
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="viewer",
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    documents = session.scalars(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id
            == principal.tenant_id,
            KnowledgeDocument.knowledge_base_id
            == knowledge_base_id,
        )
        .order_by(
            KnowledgeDocument.updated_at.desc(),
            KnowledgeDocument.id,
        )
    ).all()

    return KnowledgeDocumentListResponse(
        items=[
            KnowledgeDocumentSummary(
                id=document.id,
                file_name=document.file_name,
                mime_type=document.mime_type,
                status=document.status,
                version=document.version,
                chunk_count=_document_chunk_count(
                    document
                ),
                created_at=document.created_at,
                updated_at=document.updated_at,
            )
            for document in documents
        ]
    )


@router.post(
    "/{knowledge_base_id}/documents",
    response_model=KnowledgeDocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def upload_knowledge_base_document(
    knowledge_base_id: UUID,
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> KnowledgeDocumentUploadResponse:
    """暂存上传文件并创建异步索引任务。"""
    request_id = uuid4().hex
    response.headers["X-Request-ID"] = request_id
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="editor",
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    scope = RetrievalScope(
        tenant_id=principal.tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    settings = request.app.state.settings

    upload_service = DocumentUploadService(
        session=session,
        storage_dir=settings.document_storage_dir,
        max_upload_bytes=settings.max_upload_bytes,
    )

    try:
        result = upload_service.upload(
            file_name=file.filename or "",
            source=file.file,
            scope=scope,
            created_by_user_id=principal.user_id,
            request_id=request_id,
        )

    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc

    except InvalidUpload as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except TenantDocumentQuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={
                "X-Tenant-Quota-Resource": exc.resource,
                "X-Tenant-Quota-Limit": str(exc.limit),
                "X-Tenant-Quota-Used": str(exc.used),
            },
        ) from exc

    except (
        ActiveDocumentIndexingJobExists,
        DocumentUploadConflict,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="文档上传任务创建失败，请稍后重试。",
        ) from exc

    document = result.document
    indexing_job = result.indexing_job

    return KnowledgeDocumentUploadResponse(
        document=KnowledgeDocumentSummary(
            id=document.id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            status=document.status,
            version=document.version,
            chunk_count=_document_chunk_count(
                document
            ),
            created_at=document.created_at,
            updated_at=document.updated_at,
        ),
        indexing_job=DocumentIndexingJobSummary(
            id=indexing_job.id,
            status=indexing_job.status,
            target_version=(
                indexing_job.target_version
            ),
            attempt_count=(
                indexing_job.attempt_count
            ),
            max_attempts=indexing_job.max_attempts,
            created_at=indexing_job.created_at,
            updated_at=indexing_job.updated_at,
        ),
    )


@router.get(
    "/{knowledge_base_id}/indexing-jobs/{job_id}",
    response_model=DocumentIndexingJobDetail,
)
def get_document_indexing_job(
    knowledge_base_id: UUID,
    job_id: UUID,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> DocumentIndexingJobDetail:
    """查询当前知识库中的文档索引任务。"""
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="editor",
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    scope = RetrievalScope(
        tenant_id=principal.tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    try:
        job = DocumentIndexingJobService(
            session=session
        ).get(
            scope=scope,
            job_id=job_id,
        )

    except DocumentIndexingJobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return _indexing_job_detail(job)


@router.get(
    "/{knowledge_base_id}/indexing-jobs",
    response_model=DocumentIndexingJobListResponse,
)
def list_document_indexing_jobs(
    knowledge_base_id: UUID,
    job_status: str | None = Query(
        default=None,
        alias="status",
    ),
    document_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> DocumentIndexingJobListResponse:
    """列出当前知识库中最近的文档索引任务。"""
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="editor",
        )
    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    scope = RetrievalScope(
        tenant_id=principal.tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    try:
        jobs = DocumentIndexingJobService(
            session=session
        ).list_jobs(
            scope=scope,
            status=job_status,
            document_id=document_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return DocumentIndexingJobListResponse(
        items=[
            _indexing_job_detail(job)
            for job in jobs
        ]
    )


@router.post(
    "/{knowledge_base_id}/indexing-jobs/{job_id}/retry",
    response_model=DocumentIndexingJobDetail,
)
def retry_document_indexing_job(
    knowledge_base_id: UUID,
    job_id: UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> DocumentIndexingJobDetail:
    """把保留了候选文件的最终失败任务重新加入队列。"""
    request_id = uuid4().hex
    response.headers["X-Request-ID"] = request_id
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="editor",
        )
    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    scope = RetrievalScope(
        tenant_id=principal.tenant_id,
        knowledge_base_id=knowledge_base_id,
    )
    service = DocumentIndexingJobService(
        session=session
    )

    try:
        job = service.get(scope=scope, job_id=job_id)

        if job.status != "failed":
            raise InvalidJobTransition(
                "只有最终失败的任务可以人工重试。"
            )

        file_service = ManagedDocumentFileService(
            storage_dir=(
                request.app.state.settings
                .document_storage_dir
            )
        )

        def validate_candidate(
            locked_job: DocumentIndexingJob,
        ) -> None:
            file_service.require_staged_candidate(
                staged_storage_uri=(
                    locked_job.staged_storage_uri
                ),
                scope=scope,
                expected_content_hash=(
                    locked_job.candidate_content_hash
                ),
            )

        job = service.retry_failed(
            scope=scope,
            job_id=job_id,
            candidate_validator=validate_candidate,
            actor_user_id=principal.user_id,
            request_id=request_id,
        )
    except DocumentIndexingJobNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (
        DocumentIndexingJobConflict,
        InvalidJobTransition,
        FileNotFoundError,
        ManagedDocumentPathError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return _indexing_job_detail(job)


@router.delete(
    "/{knowledge_base_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_knowledge_base_document(
    knowledge_base_id: UUID,
    document_id: UUID,
    request: Request,
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> Response:
    """删除指定知识库中的文档。"""
    request_id = uuid4().hex
    authorization = AuthorizationService(session)

    try:
        authorization.require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="editor",
        )

    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    scope = RetrievalScope(
        tenant_id=principal.tenant_id,
        knowledge_base_id=knowledge_base_id,
    )

    deletion_service = DocumentDeletionService(
        session=session,
        storage_dir=(
            request.app.state.settings
            .document_storage_dir
        ),
    )

    try:
        deletion_service.delete(
            scope=scope,
            document_id=document_id,
            actor_user_id=principal.user_id,
            request_id=request_id,
        )

    except DocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail="文档删除失败，请稍后重试。",
        ) from exc

    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Request-ID": request_id},
    )


@router.get(
    "/{knowledge_base_id}/audit-events",
    response_model=AuditEventListResponse,
)
def list_knowledge_base_audit_events(
    knowledge_base_id: UUID,
    action: str | None = None,
    outcome: str | None = None,
    before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(
        get_current_principal
    ),
    session: Session = Depends(
        get_database_session
    ),
) -> AuditEventListResponse:
    """仅允许知识库管理员查询租户隔离的审计事件。"""
    try:
        AuthorizationService(session).require_permission(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            required_permission="admin",
        )
    except AuthorizationDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    try:
        events = AuditService(
            session
        ).list_for_knowledge_base(
            tenant_id=principal.tenant_id,
            knowledge_base_id=knowledge_base_id,
            action=action,
            outcome=outcome,
            before=before,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return AuditEventListResponse(
        items=[
            AuditEventSummary(
                id=event.id,
                actor_user_id=event.actor_user_id,
                knowledge_base_id=event.knowledge_base_id,
                action=event.action,
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                outcome=event.outcome,
                request_id=event.request_id,
                details=event.details_json,
                occurred_at=event.occurred_at,
            )
            for event in events
        ]
    )
