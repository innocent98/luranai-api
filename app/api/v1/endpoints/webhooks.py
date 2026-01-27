"""Webhook endpoints for external integrations."""

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.paystack_service import PaystackService
from app.services.webhook_handler_service import WebhookHandlerService

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
