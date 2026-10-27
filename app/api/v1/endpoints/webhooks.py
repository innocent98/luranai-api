"""Webhook endpoints for external integrations."""

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.channels import signing as whatsapp_signing
from app.core.config import settings
from app.schemas.whatsapp import WebhookEvent
from app.services import whatsapp_enqueue
from app.services.paystack_service import PaystackService
from app.services.whatsapp_inbound import get_connection_by_session_key
from app.services.webhook_handler_service import WebhookHandlerService
from app.utils.http_rate_limiter import webhook_rate_limit

router = APIRouter()


@router.post(
    "/paystack",
    status_code=status.HTTP_200_OK,
    summary="Paystack webhook endpoint",
    description="""
    Receives webhook events from Paystack.

    Events handled:
    - charge.success: Payment successful, upgrade subscription
    - subscription.create: Subscription created on Paystack
    - subscription.disable: Subscription cancelled
    - subscription.not_renew: Subscription set to not renew
    - invoice.payment_failed: Recurring payment failed

    Webhook URL: {BASE_URL}/api/v1/webhooks/paystack
    """,
    dependencies=[Depends(webhook_rate_limit)],
)
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str = Header(..., description="Paystack signature header"),
    db: Session = Depends(get_db),
):
    """
    Handle Paystack webhook events.

    Verifies webhook signature and processes events.

    Args:
        request: FastAPI request with raw body
        x_paystack_signature: Paystack signature header
        db: Database session

    Returns:
        200 OK if event processed successfully
        400 Bad Request if signature invalid
        500 Internal Server Error if processing fails
    """
    # Get raw body for signature verification
    body = await request.body()

    # Verify webhook signature
    paystack_service = PaystackService(db)
    is_valid = paystack_service.verify_webhook_signature(body, x_paystack_signature)

    if not is_valid:
        logger.warning("Invalid Paystack webhook signature")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Invalid signature"},
        )

    # Parse event data
    try:
        event_data = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse webhook body: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Invalid JSON"},
        )

    event_type = event_data.get("event")
    data = event_data.get("data", {})

    if not event_type:
        logger.warning("Webhook event missing 'event' field")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "Missing event type"},
        )

    logger.info(f"Received Paystack webhook: {event_type}")

    # Process event
    try:
        webhook_handler = WebhookHandlerService(db)
        result = webhook_handler.handle_event(event_type, data)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "success",
                "message": "Webhook processed",
                "result": result,
            },
        )

    except Exception as e:
        logger.error(f"Failed to process webhook: {e}")
        # Return 200 to prevent Paystack from retrying
        # Log error for manual investigation
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "error",
                "message": "Event logged for manual review",
            },
        )


@router.get(
    "/paystack/health",
    status_code=status.HTTP_200_OK,
    summary="Webhook health check",
    description="Simple health check endpoint for webhook configuration testing.",
    dependencies=[Depends(webhook_rate_limit)],
)
async def webhook_health():
    """
    Health check endpoint for webhook configuration.

    Returns:
        200 OK with status message
    """
    return {
        "status": "ok",
        "message": "Webhook endpoint is configured and ready to receive events",
    }


MAX_WHATSAPP_WEBHOOK_BYTES = 256 * 1024


@router.post(
    "/whatsapp/linked",
    status_code=status.HTTP_200_OK,
    summary="WhatsApp linked-device sidecar webhook",
    description="Signed sidecar events (session.status, message.inbound, "
    "message.outbound_external). Header `X-Luran-Signature: t=<unix>,v1=<hmac>`.",
)
async def whatsapp_linked_webhook(
    request: Request,
    x_luran_signature: str | None = Header(None, alias=whatsapp_signing.SIGNATURE_HEADER),
    db: Session = Depends(get_db),
):
    """Verify, validate, resolve the tenant server-side, enqueue, answer 200 fast.

    Authenticated only by the HMAC (no IP rate limit: a bad signature costs one HMAC). The
    event never names a tenant: `session_key` is looked up here and again in the job.
    Nothing derived from the signature (expected or received) is ever logged.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_WHATSAPP_WEBHOOK_BYTES:
        return JSONResponse(status_code=413, content={"error": "payload_too_large"})
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():  # stop reading as soon as the cap is passed
        received += len(chunk)
        if received > MAX_WHATSAPP_WEBHOOK_BYTES:
            return JSONResponse(status_code=413, content={"error": "payload_too_large"})
        chunks.append(chunk)
    raw = b"".join(chunks)

    secrets = [settings.WHATSAPP_WEBHOOK_SECRET, settings.WHATSAPP_WEBHOOK_SECRET_PREVIOUS]
    if not settings.WHATSAPP_ENABLED or not any(secrets):
        return JSONResponse(status_code=503, content={"error": "whatsapp_disabled"})
    if not whatsapp_signing.verify(secrets, raw, x_luran_signature):
        logger.warning("Invalid WhatsApp webhook signature")
        return JSONResponse(status_code=401, content={"error": "invalid_signature"})

    try:
        event = TypeAdapter(WebhookEvent).validate_json(raw)
    except ValidationError:
        return JSONResponse(status_code=400, content={"error": "invalid_event"})

    if get_connection_by_session_key(db, event.session_key) is None:
        # Unknown session (deleted connection, stale sidecar): acknowledge, do nothing.
        return {"status": "ignored"}

    try:
        await whatsapp_enqueue.enqueue_event(event.model_dump(mode="json"))
    except Exception:  # noqa: BLE001
        logger.exception("whatsapp webhook enqueue failed")
        return JSONResponse(status_code=503, content={"error": "queue_unavailable"})
    return {"status": "accepted"}
