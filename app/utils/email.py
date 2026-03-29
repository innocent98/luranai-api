"""Email utility for sending notifications."""

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
        logger.warning(
            f"Email not configured (SMTP_HOST={settings.SMTP_HOST}, FROM_EMAIL={settings.EMAILS_FROM_EMAIL}), skipping email send"
        )
        return False

    logger.debug(
        f"Attempting to send email to {email_to} via {settings.SMTP_HOST}:{settings.SMTP_PORT}"
    )

    try:
        from emails import Message

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

        logger.debug("Message prepared, sending via SMTP...")

        # Port 465 expects implicit SSL. Port 587 typically uses STARTTLS.
        smtp_config = {
            "host": settings.SMTP_HOST,
            "port": settings.SMTP_PORT,
            "user": settings.SMTP_USER,
            "password": settings.SMTP_PASSWORD,
        }
        if settings.SMTP_TLS:
            if settings.SMTP_PORT == 465:
                smtp_config["ssl"] = True
            else:
                smtp_config["tls"] = True

        # Send email
        response = message.send(to=email_to, smtp=smtp_config)

        # Check response status
        status_code = getattr(response, "status_code", None)

        logger.debug(f"SMTP response received with status_code={status_code}")

        if status_code == 250:
            logger.info("Email sent successfully")
            return True
        elif status_code is None:
            # Check if response has error attribute
            error = getattr(response, "error", None)
            if error:
                logger.error("Email send failed without status code")
            else:
                logger.error("Email send failed without status code")
            return False
        else:
            logger.warning(f"Email send failed with status {status_code}")
            return False

    except ImportError:
        logger.error("✗ emails library not installed. Install with: pip install emails")
        return False
    except Exception:
        logger.error("Exception while sending email")
        return False


async def send_job_completion_email(
    email_to: str,
    user_name: str,
    tenant_name: str,
    job_status: str,
    document_name: str,
    chunks_created: int | None = None,
    error_message: str | None = None,
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
    customer_contact_email: str | None = None,
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
        customer_contact_email: Captured customer follow-up email if available

    Returns:
        True if sent successfully
    """
    subject = f"🎫 New Support Ticket: {ticket_number} ({ticket_priority} Priority)"

    customer_contact_line = (
        f"Customer Contact Email: {customer_contact_email}\n"
        if customer_contact_email
        else "Customer Contact Email: Not provided\n"
    )

    body = f"""
Hello {user_name},

A customer conversation has been escalated and requires your attention.

TICKET DETAILS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ticket Number: {ticket_number}
Priority: {ticket_priority}
Tenant: {tenant_name}
Escalation Reason: {escalation_reason}
{customer_contact_line}

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
