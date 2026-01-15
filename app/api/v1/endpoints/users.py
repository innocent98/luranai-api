"""User management endpoints for TENANT_ADMIN."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.services.user_management_service import UserManagementService
from app.schemas.user import (
    UserInviteRequest,
    UserResponse,
    UserListResponse,
    UserUpdate,
    UserRegeneratePasswordResponse,
)

router = APIRouter()


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def invite_team_member(
    invite_data: UserInviteRequest,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Invite a new team member (TENANT_ADMIN only).

    Creates a new user with SUPPORT_AGENT or VIEWER role and sends invite email.

    Args:
        invite_data: User invitation data
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Created user response

    Raises:
        HTTPException: 400 if user already exists or invalid role
        HTTPException: 403 if not tenant admin
    """
    user_service = UserManagementService(db)

    try:
        user = user_service.invite_user(
            invite_data=invite_data,
            tenant_id=current_user.tenant_id,
            invited_by_name=current_user.full_name,
        )
        return user
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get(
    "",
    response_model=list[UserListResponse],
    status_code=status.HTTP_200_OK,
)
def list_team_members(
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    List all team members in the tenant.

    Args:
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        List of users in the tenant

    Raises:
        HTTPException: 403 if not tenant admin
    """
    user_service = UserManagementService(db)
    users = user_service.list_users(tenant_id=current_user.tenant_id)
    return users


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
)
def get_team_member(
    user_id: UUID,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Get team member details by ID.

    Args:
        user_id: User UUID
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        User details

    Raises:
        HTTPException: 404 if user not found
        HTTPException: 403 if not tenant admin
    """
    user_service = UserManagementService(db)
    user = user_service.get_user_by_id(
        user_id=user_id,
        tenant_id=current_user.tenant_id,
    )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return user


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
)
def update_team_member(
    user_id: UUID,
    update_data: UserUpdate,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Update team member information.

    Args:
        user_id: User UUID
        update_data: User update data
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Updated user details

    Raises:
        HTTPException: 400 if invalid update
        HTTPException: 404 if user not found
        HTTPException: 403 if not tenant admin
    """
    user_service = UserManagementService(db)

    try:
        user = user_service.update_user(
            user_id=user_id,
            tenant_id=current_user.tenant_id,
            update_data=update_data,
        )
        return user
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_200_OK,
)
def deactivate_team_member(
    user_id: UUID,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Deactivate a team member (soft delete).

    Args:
        user_id: User UUID
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: 400 if trying to deactivate TENANT_ADMIN
        HTTPException: 404 if user not found
        HTTPException: 403 if not tenant admin
    """
    user_service = UserManagementService(db)

    try:
        user_service.deactivate_user(
            user_id=user_id,
            tenant_id=current_user.tenant_id,
        )
        return {"message": "User deactivated successfully"}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/{user_id}/regenerate-password",
    response_model=UserRegeneratePasswordResponse,
    status_code=status.HTTP_200_OK,
)
def regenerate_user_password(
    user_id: UUID,
    current_user: UserResponse = Depends(get_current_tenant_admin),
    db: Session = Depends(get_db),
):
    """
    Regenerate team member's password.

    Generates a new temporary password and sends it via email.

    Args:
        user_id: User UUID
        current_user: Current authenticated tenant admin
        db: Database session

    Returns:
        Response with new temporary password

    Raises:
        HTTPException: 404 if user not found
        HTTPException: 403 if not tenant admin
    """
    user_service = UserManagementService(db)

    try:
        response = user_service.regenerate_password(
            user_id=user_id,
            tenant_id=current_user.tenant_id,
        )
        return response
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
