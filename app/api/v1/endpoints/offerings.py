"""Offerings catalog API (`/api/v1/offerings`): items the AI sells from on every channel.

Reads are open to any staff role (and to a lapsed tenant); writes need a tenant admin and a plan
that still includes the feature. Errors use the module's flat `{code, message[, ...]}` body.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_current_user, get_db
from app.schemas.offering import (
    ImageOrder,
    ImportRequest,
    OfferingCreate,
    OfferingPatch,
    SettingsPatch,
)
from app.schemas.user import UserResponse
from app.services.entitlements import get_entitlements_for_tenant_id
from app.services.offering_import import OfferingImportService
from app.services.offering_images import MAX_UPLOAD_BYTES, OfferingImageService
from app.services.offering_settings import OfferingSettingsService
from app.services.offering_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    OfferingError,
    OfferingService,
)

from app.utils.http_rate_limiter import offering_upload_rate_limit

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


# Static paths (import, and later settings/misses/confirm) come before `/{offering_id}`: a UUID
# path parameter would otherwise answer 422 for them.
@router.post("/import/preview")
def preview_import(
    body: ImportRequest,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Parse a pasted list or CSV and say what an import would do. Nothing is saved."""
    entitlements = get_entitlements_for_tenant_id(db, user.tenant_id)
    if entitlements is None:
        raise OfferingError(404, "offering_not_found", "Offering not found")
    data = OfferingImportService(db).preview(
        user.tenant_id, entitlements, body.format, body.content, body.mode
    )
    return {"data": data}


@router.post("/import")
def apply_import(
    body: ImportRequest,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Apply an import in one transaction (all valid rows or none, unless `skip_errors`)."""
    data = OfferingImportService(db).apply(
        user.tenant_id, body.format, body.content, body.mode, body.skip_errors
    )
    return {"data": data}


@router.post("/confirm")
def confirm_offerings(
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """ "Everything is still correct today": stamps the time and resets the staleness caveat."""
    return {"data": OfferingSettingsService(db).confirm(user.tenant_id)}


@router.get("/settings")
def get_offering_settings(
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_user),
):
    return {"data": OfferingSettingsService(db).get(user.tenant_id)}


@router.patch("/settings")
def patch_offering_settings(
    body: SettingsPatch,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    return {"data": OfferingSettingsService(db).patch(user.tenant_id, body.model_dump())}


@router.get("/misses")
def list_offering_misses(
    days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_user),
):
    """What customers asked for that is not listed (term, count, last asked), most wanted first."""
    return {"data": OfferingSettingsService(db).list_misses(user.tenant_id, days)}


@router.delete("/misses/{miss_id}", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_offering_miss(
    miss_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    OfferingSettingsService(db).dismiss_miss(user.tenant_id, miss_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


# ----------------------------------------------------------------------------- photos
@router.post(
    "/{offering_id}/images",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(offering_upload_rate_limit)],
)
async def upload_offering_image(
    offering_id: UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Add a photo (JPEG, PNG or WEBP, at most 5 MB). It is checked, moderated and optimised."""
    content = await file.read(MAX_UPLOAD_BYTES + 1)  # bounded read: never buffer more than the cap
    data = await OfferingImageService(db).upload(user.tenant_id, offering_id, content)
    return {"data": data}


@router.put("/{offering_id}/images/order")
def reorder_offering_images(
    offering_id: UUID,
    body: ImageOrder,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    """Set the photo order in one transaction; the first id becomes the cover."""
    return {"data": OfferingImageService(db).reorder(user.tenant_id, offering_id, body.image_ids)}


@router.delete("/{offering_id}/images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_offering_image(
    offering_id: UUID,
    image_id: UUID,
    db: Session = Depends(get_db),
    user: UserResponse = Depends(get_current_tenant_admin),
):
    OfferingImageService(db).delete(user.tenant_id, offering_id, image_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
