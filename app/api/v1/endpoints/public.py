"""Public endpoints for hosted support page, directory, and visitor bootstrap."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.explore import TenantCategory
from app.schemas.visitor import (
    PublicBusinessDirectoryResponse,
    PublicBusinessProfileResponse,
    VisitorBootstrapRequest,
    VisitorBootstrapResponse,
)
from app.services.visitor_service import VisitorService

router = APIRouter()


@router.get(
    "/businesses",
    response_model=PublicBusinessDirectoryResponse,
    status_code=status.HTTP_200_OK,
)
def list_public_businesses(
    q: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    category: TenantCategory | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """List publicly discoverable businesses for the customer support directory."""
    visitor_service = VisitorService(db)
    return visitor_service.list_public_businesses(
        query=q, page=page, page_size=page_size, category=category,
    )


@router.get(
    "/business/{slug}",
    response_model=PublicBusinessProfileResponse,
    status_code=status.HTTP_200_OK,
)
def get_public_business(
    slug: str,
    db: Session = Depends(get_db),
):
    """Resolve a public business profile by hosted support slug."""
    visitor_service = VisitorService(db)
    tenant = visitor_service.get_public_business_profile(slug)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
    return tenant


@router.post(
    "/visitor/bootstrap",
    response_model=VisitorBootstrapResponse,
    status_code=status.HTTP_200_OK,
)
def bootstrap_visitor(
    payload: VisitorBootstrapRequest,
    db: Session = Depends(get_db),
):
    """Resolve or create the public visitor identity and visitor session."""
    visitor_service = VisitorService(db)
    try:
        return visitor_service.bootstrap_visitor(payload)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
