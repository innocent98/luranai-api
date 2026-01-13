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
        logger.debug("Email not configured, skipping email send")
        return False

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

        if response.status_code == 250:
            logger.info(f"Email sent successfully to {email_to}")
            return True
        else:
            logger.warning(f"Email send failed with status {response.status_code}")
            return False

    except ImportError:
        logger.error("emails library not installed. Install with: pip install emails")
        return False
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
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
