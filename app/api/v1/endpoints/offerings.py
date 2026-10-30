"""Offerings catalog API (`/api/v1/offerings`): items the AI sells from on every channel.

Reads are open to any staff role (and to a lapsed tenant); writes need a tenant admin and a plan
that still includes the feature. Errors use the module's flat `{code, message[, ...]}` body.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_current_user, get_db
from app.schemas.offering import OfferingCreate, OfferingPatch
from app.schemas.user import UserResponse
from app.services.entitlements import get_entitlements_for_tenant_id
from app.services.offering_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    OfferingError,
    OfferingService,
)

router = APIRouter()


@router.get("")
def list_offerings(
    status_filter: Literal["active", "archived"] = Query("active", alias="status"),
    kind: Literal["product", "service"] | None = None,
    category: str | None = None,
    availability: Literal["in_stock", "limited", "out_of_stock", "on_request"] | None = None,
    q: str | None = Query(None, max_length=120),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = None,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_user),
):
    entitlements = get_entitlements_for_tenant_id(db, user.tenant_id)
    if entitlements is None:
        raise OfferingError(404, "offering_not_found", "Offering not found")
    data, meta = OfferingService(db).list_items(
        user.tenant_id,
        entitlements,
        status=status_filter,
        kind=kind,
        category=category,
        availability=availability,
        q=q,
        limit=limit,
        cursor=cursor,
    )
    return {"data": data, "meta": meta}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_offering(
    body: OfferingCreate,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    return {"data": OfferingService(db).create(user.tenant_id, body.model_dump())}


@router.get("/{offering_id}")
def get_offering(
    offering_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_user),
):
    return {"data": OfferingService(db).get(user.tenant_id, offering_id)}


@router.patch("/{offering_id}")
def patch_offering(
    offering_id: UUID,
    body: OfferingPatch,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    changes = body.model_dump(include=body.model_fields_set)
    return {"data": OfferingService(db).patch(user.tenant_id, offering_id, changes)}


@router.delete("/{offering_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_offering(
    offering_id: UUID,
    hard: bool = False,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Archive the item; with `?hard=true` delete the row and its photos for good."""
    service = OfferingService(db)
    if hard:
        service.hard_delete(user.tenant_id, offering_id)
    else:
        service.archive(user.tenant_id, offering_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
