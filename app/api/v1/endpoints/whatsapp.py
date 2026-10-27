"""WhatsApp channel: tenant-admin connection API (contract section B)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant_admin, get_db
from app.schemas.user import UserResponse
from app.schemas.whatsapp import (
    ConnectionCreate,
    ConnectionCreated,
    ConnectionOut,
    ConnectionPatch,
    ConsentOut,
    PairingCodeOut,
)
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
):
    """Start linking a number. The pairing code is in this response ONLY (never stored)."""
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
