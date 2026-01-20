"""Email utility for sending notifications."""

from typing import Optional
from loguru import logger

from app.core.config import settings


async def send_email(
    email_to: str,
    subject: str,
    body: str,
    html: bool = False,
) -> bool:
    """
    Send email notification.

    Args:
        email_to: Recipient email address
        subject: Email subject
        body: Email body (text or HTML)
        html: Whether body is HTML (default: False)

    Returns:
        True if email sent successfully, False otherwise
    """
    # Check if email is configured
    if not settings.SMTP_HOST or not settings.EMAILS_FROM_EMAIL:
        logger.warning(f"Email not configured (SMTP_HOST={settings.SMTP_HOST}, FROM_EMAIL={settings.EMAILS_FROM_EMAIL}), skipping email send")
        return False

    logger.debug(f"Attempting to send email to {email_to} via {settings.SMTP_HOST}:{settings.SMTP_PORT}")

    try:
        from emails import Message
        from emails.template import JinjaTemplate

        # Prepare message
        if html:
            message = Message(
                subject=subject,
                html=body,
                mail_from=(settings.EMAILS_FROM_NAME or "Luran", settings.EMAILS_FROM_EMAIL),
            )
        else:
            # Convert plain text to HTML
            html_body = f"<pre>{body}</pre>"
            message = Message(
                subject=subject,
                html=html_body,
                mail_from=(settings.EMAILS_FROM_NAME or "Luran", settings.EMAILS_FROM_EMAIL),
            )

        logger.debug(f"Message prepared, sending via SMTP...")

        # Send email
        response = message.send(
            to=email_to,
            smtp={
                'host': settings.SMTP_HOST,
                'port': settings.SMTP_PORT,
                'tls': settings.SMTP_TLS,
                'user': settings.SMTP_USER,
                'password': settings.SMTP_PASSWORD,
            }
        )

        # Check response status
        status_code = getattr(response, 'status_code', None)

        logger.debug(f"SMTP response: status_code={status_code}, response={response}")

        if status_code == 250:
            logger.info(f"✓ Email sent successfully to {email_to}")
            return True
        elif status_code is None:
            # Check if response has error attribute
            error = getattr(response, 'error', None)
            if error:
                logger.error(f"✗ Email send failed to {email_to}: {error}")
            else:
                # Try to get more details from response
                response_dict = {}
                if hasattr(response, '__dict__'):
                    response_dict = {k: v for k, v in response.__dict__.items() if not k.startswith('_')}
                logger.error(f"✗ Email send failed to {email_to}: No status code returned. Response: {response_dict}")
                logger.error(f"  SMTP Config: host={settings.SMTP_HOST}, port={settings.SMTP_PORT}, tls={settings.SMTP_TLS}, user={settings.SMTP_USER}")
            return False
        else:
            logger.warning(f"✗ Email send failed to {email_to} with status {status_code}")
            return False

    except ImportError:
        logger.error("✗ emails library not installed. Install with: pip install emails")
        return False
    except Exception as e:
        logger.error(f"✗ Exception while sending email to {email_to}: {type(e).__name__}: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False


async def send_job_completion_email(
    email_to: str,
    user_name: str,
    tenant_name: str,
    job_status: str,
    document_name: str,
    chunks_created: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """
    Send job completion notification email.

    Args:
        email_to: Recipient email
        user_name: User's full name
        tenant_name: Tenant display name
        job_status: Job status (completed/failed)
        document_name: Name of processed document
        chunks_created: Number of chunks created (if successful)
        error_message: Error message (if failed)

    Returns:
        True if sent successfully
    """
    if job_status == "completed":
        subject = "✓ Document Processing Complete"
        body = f"""
Hello {user_name},

Your document has been successfully processed and is now available in the knowledge base.

Details:
- Document: {document_name}
- Tenant: {tenant_name}
- Chunks Created: {chunks_created}
- Status: ✓ Complete

You can now start using this knowledge in your AI chatbot.

Best regards,
Luran Team
        """
    else:
        subject = "✗ Document Processing Failed"
        body = f"""
Hello {user_name},

Unfortunately, your document processing job failed.

Details:
- Document: {document_name}
- Tenant: {tenant_name}
- Error: {error_message}

Please try again or contact support if the issue persists.

Best regards,
Luran Team
        """

    return await send_email(email_to, subject, body)


async def send_ticket_escalation_email(
    email_to: str,
    user_name: str,
    tenant_name: str,
    ticket_number: str,
    escalation_reason: str,
    customer_query: str,
    ticket_priority: str,
    ticket_id: str,
) -> bool:
    """
    Send email notification when a conversation is escalated to a ticket.

    Args:
        email_to: Support agent or tenant admin email
        user_name: Agent/admin full name
        tenant_name: Tenant display name
        ticket_number: Generated ticket number (e.g., TKT-2026-0001)
        escalation_reason: Why the conversation was escalated
        customer_query: Last customer message
        ticket_priority: Ticket priority (LOW, MEDIUM, HIGH, URGENT)
        ticket_id: Ticket UUID

    Returns:
        True if sent successfully
    """
    subject = f"🎫 New Support Ticket: {ticket_number} ({ticket_priority} Priority)"

    body = f"""
Hello {user_name},

A customer conversation has been escalated and requires your attention.

TICKET DETAILS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ticket Number: {ticket_number}
Priority: {ticket_priority}
Tenant: {tenant_name}
Escalation Reason: {escalation_reason}

CUSTOMER MESSAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"{customer_query}"

NEXT STEPS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Review the full conversation in your dashboard
2. Respond to the customer directly
3. Mark the ticket as resolved when complete

View Ticket: [Your Dashboard URL]/tickets/{ticket_id}

This customer is waiting for your response. Please respond as soon as possible.

Best regards,
Luran Support System
    """

    return await send_email(email_to, subject, body)
