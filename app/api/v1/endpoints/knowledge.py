"""Knowledge management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_tenant_admin,
    get_db,
    verify_subscription_active,
    verify_tenant_access,
)
from app.api.v1.endpoints.knowledge_upload import router as upload_router
from app.db.models.knowledge import SourceStatus
from app.schemas.knowledge import (
    DocumentUploadRequest,
    KnowledgeChunkResponse,
    KnowledgeSourceDetailResponse,
    KnowledgeSourceResponse,
)
from app.schemas.user import UserResponse
from app.services.knowledge_service import KnowledgeService
from app.utils.http_rate_limiter import upload_rate_limit
from app.utils.knowledge_failures import KnowledgeError

router = APIRouter()

router.include_router(upload_router, tags=["knowledge"])


@router.get(
    "/tenants/{tenant_id}/sources",
    response_model=list[KnowledgeSourceResponse],
    status_code=status.HTTP_200_OK,
)
def list_knowledge_sources(
    tenant_id: UUID,
    status_filter: SourceStatus | None = Query(None, description="Filter by status"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    List all knowledge sources for a tenant.

    Args:
        tenant_id: Tenant UUID
        status_filter: Optional status filter
        db: Database session
        current_user: Current admin user

    Returns:
        List of knowledge sources
    """
    # Verify access
    verify_tenant_access(tenant_id, current_user)

    knowledge_service = KnowledgeService(db)
    sources = knowledge_service.get_tenant_sources(tenant_id, status=status_filter)

    return sources


@router.get(
    "/sources/{source_id}",
    response_model=KnowledgeSourceDetailResponse,
    status_code=status.HTTP_200_OK,
)
def get_knowledge_source(
    source_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get knowledge source details with chunks.

    Args:
        source_id: Source UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Knowledge source with chunks
    """
    knowledge_service = KnowledgeService(db)
    source = knowledge_service.get_source(source_id, include_chunks=True)

    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge source not found"
        )

    # Verify tenant access
    verify_tenant_access(source.tenant_id, current_user)

    return source


@router.delete("/sources/{source_id}", status_code=status.HTTP_200_OK)
def delete_knowledge_source(
    source_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Delete (deactivate) a knowledge source and its chunks.

    Args:
        source_id: Source UUID
        db: Database session
        current_user: Current admin user

    Returns:
        Success message
    """
    knowledge_service = KnowledgeService(db)

    # Get source to verify tenant access
    source = knowledge_service.get_source(source_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge source not found"
        )

    # Verify tenant access
    verify_tenant_access(source.tenant_id, current_user)

    try:
        knowledge_service.delete_source(source_id)
        return {"message": "Knowledge source deleted successfully"}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.get(
    "/sources/{source_id}/chunks",
    response_model=list[KnowledgeChunkResponse],
    status_code=status.HTTP_200_OK,
)
def get_source_chunks(
    source_id: UUID,
    active_only: bool = Query(True, description="Return only active chunks"),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """
    Get all chunks for a knowledge source.

    Args:
        source_id: Source UUID
        active_only: Whether to return only active chunks
        db: Database session
        current_user: Current admin user

    Returns:
        List of knowledge chunks
    """
    knowledge_service = KnowledgeService(db)

    # Get source to verify tenant access
    source = knowledge_service.get_source(source_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge source not found"
        )

    # Verify tenant access
    verify_tenant_access(source.tenant_id, current_user)

    chunks = knowledge_service.get_source_chunks(source_id, active_only=active_only)
    return chunks


@router.post(
    "/upload",
    response_model=KnowledgeSourceResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_document(
    upload_request: DocumentUploadRequest,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: None = Depends(verify_subscription_active),
):
    """
    Upload and process a document.

    This endpoint orchestrates the full document ingestion pipeline:
    1. Validates document content (422 `document_too_short` / `no_text_extracted`, before any
       source is created)
    2. Chunks the text into overlapping segments
    3. Generates embeddings for each chunk
    4. Stores chunks in PostgreSQL
    5. Stores vectors in Qdrant

    Args:
        upload_request: Document upload request with text content
        db: Database session
        current_user: Current admin user

    Returns:
        Created knowledge source with metadata

    Raises:
        HTTPException: If upload or processing fails
    """
    # Verify tenant access
    verify_tenant_access(upload_request.tenant_id, current_user)

    knowledge_service = KnowledgeService(db)

    # Same plan-cap guard as the file and URL uploads (FAILED/INACTIVE sources do not count).
    knowledge_service.check_knowledge_source_limit(upload_request.tenant_id, source_category="file")

    try:
        # Process document through ingestion pipeline
        source = await knowledge_service.process_document(
            tenant_id=upload_request.tenant_id,
            document_text=upload_request.document_text,
            document_name=upload_request.document_name,
            source_type=upload_request.source_type,
            document_metadata=upload_request.metadata,
            uploaded_by=current_user.id,
        )

        return source

    except KnowledgeError:
        raise  # rendered as the flat `{code, message}` body by the app-level handler
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Document processing failed: {str(e)}"
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error during document processing: {str(e)}",
        ) from e
