"""WhatsApp channel: tenant-admin connection API (contract section B)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db, verify_subscription_active
from app.schemas.user import UserResponse
from app.schemas.whatsapp import (
    ConnectionCreate,
    ConnectionCreated,
    ConnectionOut,
    ConnectionPatch,
    ConsentOut,
    ExclusionBulkCreate,
    ExclusionBulkOut,
    ExclusionCreate,
    ExclusionOut,
    ExclusionPage,
    PairingCodeOut,
)
from app.services.whatsapp_exclusion_service import WhatsAppExclusionService, exclusion_to_dict
from app.services.whatsapp_service import (
    CONSENT_TEXT,
    CONSENT_VERSION,
    WhatsAppService,
    connection_to_dict,
)
from app.utils.http_rate_limiter import whatsapp_pairing_rate_limit

router = APIRouter()


@router.get("/consent", response_model=ConsentOut)
def get_consent(current_user: UserResponse = Depends(get_current_tenant_admin)):
    """Current consent text and its version (send the version back when creating a connection)."""
    return ConsentOut(version=CONSENT_VERSION, text=CONSENT_TEXT)


@router.post("/connections", response_model=ConnectionCreated, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: object = Depends(verify_subscription_active),
):
    """Start linking a number. The pairing code is in this response ONLY (never stored).

    402 for an expired trial or an inactive subscription: a lapsed tenant must not take one of the
    few platform slots. Listing, patching and deleting stay open so a lapsed tenant can free one.
    """
    created = await WhatsAppService(db).create_connection(
        tenant_id=current_user.tenant_id,
        user_id=current_user.id,
        phone_number=body.phone_number,
        label=body.label,
        consent_version=body.consent.version if body.consent else None,
        consent_accepted=bool(body.consent and body.consent.accepted),
    )
    return ConnectionCreated(
        id=str(created.connection.id),
        status="awaiting_pair",
        pairing_code=created.pairing.pairing_code,
        expires_in_s=created.pairing.expires_in_s,
    )


@router.get("/connections", response_model=list[ConnectionOut])
def list_connections(
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    rows = WhatsAppService(db).list_for_tenant(current_user.tenant_id)
    return [connection_to_dict(c) for c in rows]


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
def get_connection(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    return connection_to_dict(
        WhatsAppService(db).get_for_tenant(current_user.tenant_id, connection_id)
    )


@router.post(
    "/connections/{connection_id}/pairing-code",
    response_model=PairingCodeOut,
    dependencies=[Depends(whatsapp_pairing_rate_limit)],
)
async def new_pairing_code(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
    _subscription_check: object = Depends(verify_subscription_active),
):
    pairing = await WhatsAppService(db).new_pairing_code(current_user.tenant_id, connection_id)
    return PairingCodeOut(pairing_code=pairing.pairing_code, expires_in_s=pairing.expires_in_s)


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
def patch_connection(
    connection_id: UUID,
    body: ConnectionPatch,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    conn = WhatsAppService(db).patch(
        current_user.tenant_id,
        connection_id,
        ai_enabled=body.ai_enabled,
        label=body.label,
        label_set="label" in body.model_fields_set,
    )
    return connection_to_dict(conn)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    await WhatsAppService(db).delete(current_user.tenant_id, connection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------- contact exclusions
@router.post(
    "/connections/{connection_id}/exclusions",
    response_model=ExclusionOut,
    status_code=status.HTTP_201_CREATED,
)
def add_exclusion(
    connection_id: UUID,
    body: ExclusionCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    """Luran will ignore this contact on this number entirely (no reply, nothing stored)."""
    entry = WhatsAppExclusionService(db).add(
        current_user.tenant_id, connection_id, body.phone_number, body.label
    )
    return exclusion_to_dict(entry)


@router.post("/connections/{connection_id}/exclusions/bulk", response_model=ExclusionBulkOut)
def add_exclusions_bulk(
    connection_id: UUID,
    body: ExclusionBulkCreate,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    result = WhatsAppExclusionService(db).add_bulk(
        current_user.tenant_id,
        connection_id,
        [(e.phone_number, e.label) for e in body.entries],
    )
    return ExclusionBulkOut(
        added=result.added, already_excluded=result.already_excluded, rejected=result.rejected
    )


@router.get("/connections/{connection_id}/exclusions", response_model=ExclusionPage)
def list_exclusions(
    connection_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    rows, total = WhatsAppExclusionService(db).list(
        current_user.tenant_id, connection_id, limit, offset
    )
    return ExclusionPage(
        items=[exclusion_to_dict(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.delete(
    "/connections/{connection_id}/exclusions/{exclusion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_exclusion(
    connection_id: UUID,
    exclusion_id: UUID,
    db: Session = Depends(get_db),
    current_user: UserResponse = Depends(get_current_tenant_admin),
):
    WhatsAppExclusionService(db).remove(current_user.tenant_id, connection_id, exclusion_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
