"""Sandbox endpoints for automation action testing and demos."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter()


def _validate_bearer_token(auth_header: str | None) -> None:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )


def _validate_api_key(x_api_key: str | None) -> None:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )


@router.get("/orders/{order_id}/status")
def track_order_status(
    order_id: str,
    authorization: str | None = Header(default=None),
    x_customer_email: str | None = Header(default=None),
):
    """Return synthetic order status data."""
    _validate_bearer_token(authorization)
    normalized_order_id = order_id.strip()
    if len(normalized_order_id) < 6 or not any(char.isdigit() for char in normalized_order_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )
    stage = "in_transit" if len(order_id) % 2 == 0 else "processing"
    eta_days = 2 if stage == "in_transit" else 4
    return {
        "order_id": normalized_order_id,
        "status": stage,
        "eta_date": (datetime.now(UTC) + timedelta(days=eta_days)).date().isoformat(),
        "customer_email": x_customer_email,
    }


@router.get("/orders/status")
def track_order_status_by_reference(
    order_reference: str,
    authorization: str | None = Header(default=None),
    x_customer_email: str | None = Header(default=None),
):
    """Return synthetic order status data using a query-based order reference."""
    return track_order_status(
        order_id=order_reference,
        authorization=authorization,
        x_customer_email=x_customer_email,
    )


@router.get("/accounts/{account_id}/status")
def get_account_status(
    account_id: str,
    email: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Return synthetic account status."""
    _validate_bearer_token(authorization)
    is_suspended = account_id.lower().startswith("susp")
    return {
        "account_id": account_id,
        "email": email,
        "status": "suspended" if is_suspended else "active",
        "suspension_reason": "multiple_failed_logins" if is_suspended else None,
        "can_self_recover": not is_suspended,
    }


@router.get("/accounts/status")
def get_account_status_by_query(
    account_id: str,
    email: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Return synthetic account status using a query-based account ID."""
    return get_account_status(
        account_id=account_id,
        email=email,
        authorization=authorization,
    )


@router.get("/accounts/lookup")
def lookup_account_by_email(
    email: str,
    authorization: str | None = Header(default=None),
):
    """Resolve a synthetic account ID from an email address."""
    _validate_bearer_token(authorization)
    local_part = email.split("@", 1)[0].strip().lower() or "customer"
    prefix = "susp" if "susp" in local_part or "lock" in local_part else "acct"
    account_id = f"{prefix}-{local_part[:24]}"
    return {
        "email": email,
        "account_id": account_id,
        "status": "suspended" if prefix == "susp" else "active",
    }


class SupportTicketRequest(BaseModel):
    customer_email: str = Field(...)
    subject: str = Field(...)
    category: str | None = None
    message: str = Field(...)


@router.post("/support/tickets")
def create_support_ticket(
    payload: SupportTicketRequest,
    authorization: str | None = Header(default=None),
):
    """Create synthetic support ticket."""
    _validate_bearer_token(authorization)
    ticket_id = f"TKT-{str(uuid4())[:8].upper()}"
    return {
        "ticket_id": ticket_id,
        "status": "open",
        "priority": "normal",
        "subject": payload.subject,
        "category": payload.category,
        "created_at": datetime.now(UTC).isoformat(),
    }


@router.post("/accounts/{account_id}/unlock")
def unlock_suspended_account(
    account_id: str,
    payload: dict,
    authorization: str | None = Header(default=None),
):
    """Perform synthetic account unlock action."""
    _validate_bearer_token(authorization)
    reason = payload.get("reason", "verified_by_support")
    return {
        "account_id": account_id,
        "status": "active",
        "action": "unlock_completed",
        "reason": reason,
        "updated_at": datetime.now(UTC).isoformat(),
    }


@router.post("/accounts/unlock")
def unlock_suspended_account_from_body(
    payload: dict,
    authorization: str | None = Header(default=None),
):
    """Perform synthetic account unlock action using body-based account ID."""
    account_id = str(payload.get("account_id", "")).strip()
    if not account_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="account_id is required",
        )
    return unlock_suspended_account(
        account_id=account_id,
        payload=payload,
        authorization=authorization,
    )


@router.get("/appointments/slots")
def check_appointment_slots(
    date: str,
    service: str,
    location: str | None = None,
    x_api_key: str | None = Header(default=None),
):
    """Return synthetic appointment slots."""
    _validate_api_key(x_api_key)
    return {
        "date": date,
        "service": service,
        "location": location,
        "slots": [
            {"time": "09:00", "available": True},
            {"time": "11:00", "available": True},
            {"time": "14:30", "available": False},
            {"time": "16:00", "available": True},
        ],
    }


class AppointmentRequest(BaseModel):
    customer_name: str = Field(...)
    customer_email: str = Field(...)
    date: str = Field(...)
    time: str = Field(...)
    service: str = Field(...)
    notes: str | None = None


@router.post("/appointments")
def create_appointment_booking(
    payload: AppointmentRequest,
    x_api_key: str | None = Header(default=None),
):
    """Create synthetic appointment booking."""
    _validate_api_key(x_api_key)
    booking_id = f"BK-{str(uuid4())[:8].upper()}"
    return {
        "booking_id": booking_id,
        "status": "confirmed",
        "customer_name": payload.customer_name,
        "customer_email": payload.customer_email,
        "date": payload.date,
        "time": payload.time,
        "service": payload.service,
    }
