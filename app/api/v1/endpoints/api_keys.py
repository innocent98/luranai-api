"""API key management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.services.api_key_service import APIKeyService
from app.schemas.api_key import (
    APIKeyCreate,
    APIKeyCreateResponse,
    APIKeyListResponse,
    APIKeyUpdate,
    APIKeyResponse,
)
from app.schemas.user import UserResponse

router = APIRouter()


@router.post(
    "",
    response_model=APIKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_api_key(
    key_data: APIKeyCreate,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Create a new API key (TENANT_ADMIN only).

    The full API key is returned only once during creation.
    Store it securely - it cannot be retrieved again.

    Args:
        key_data: API key creation data
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Created API key with full key (shown only once)

    Raises:
        HTTPException: 403 if not tenant admin
    """
    api_key_service = APIKeyService(db)

    try:
        api_key = api_key_service.create_api_key(
            tenant_id=current_user.tenant_id,
            key_data=key_data,
        )
        return api_key
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create API key: {str(e)}",
        )


@router.get(
    "",
    response_model=list[APIKeyListResponse],
    status_code=status.HTTP_200_OK,
)
def list_api_keys(
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    List all API keys for the tenant.

    Args:
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        List of API keys (without full keys)

    Raises:
        HTTPException: 403 if not tenant admin
    """
    api_key_service = APIKeyService(db)
    keys = api_key_service.list_api_keys(tenant_id=current_user.tenant_id)
    return keys


@router.get(
    "/{key_id}",
    response_model=APIKeyResponse,
    status_code=status.HTTP_200_OK,
)
def get_api_key(
    key_id: UUID,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Get API key details by ID.

    Args:
        key_id: API key UUID
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        API key details (without full key)

    Raises:
        HTTPException: 404 if not found
        HTTPException: 403 if not tenant admin
    """
    api_key_service = APIKeyService(db)
    key = api_key_service.get_api_key(
        key_id=key_id,
        tenant_id=current_user.tenant_id,
    )

    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    return APIKeyResponse.model_validate(key)


@router.patch(
    "/{key_id}",
    response_model=APIKeyResponse,
    status_code=status.HTTP_200_OK,
)
def update_api_key(
    key_id: UUID,
    update_data: APIKeyUpdate,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Update an API key.

    Args:
        key_id: API key UUID
        update_data: Update data
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Updated API key details

    Raises:
        HTTPException: 404 if not found
        HTTPException: 403 if not tenant admin
    """
    api_key_service = APIKeyService(db)
    key = api_key_service.update_api_key(
        key_id=key_id,
        tenant_id=current_user.tenant_id,
        update_data=update_data,
    )

    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    return APIKeyResponse.model_validate(key)


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_200_OK,
)
def delete_api_key(
    key_id: UUID,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Delete an API key.

    Args:
        key_id: API key UUID
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: 404 if not found
        HTTPException: 403 if not tenant admin
    """
    api_key_service = APIKeyService(db)
    deleted = api_key_service.delete_api_key(
        key_id=key_id,
        tenant_id=current_user.tenant_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    return {"message": "API key deleted successfully"}
