"""Background worker for auto-resolving and abandoning conversations."""

from datetime import datetime, timedelta
from typing import Dict, Any

from loguru import logger
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.db.models.conversation import (
    ConversationSession,
    SessionStatus,
    Ticket,
    TicketStatus,
)
from app.db.models.user import User, UserRole


async def auto_abandon_inactive_conversations(ctx: dict) -> Dict[str, Any]:
    """
    Mark inactive ACTIVE conversations as ABANDONED.

    Runs every 5 minutes.
    Marks conversations as abandoned if:
    - Status is ACTIVE
    - No customer message for 30+ minutes
    - Not handled by an agent

    Args:
        ctx: ARQ context

    Returns:
        dict with success status and abandoned count
    """
    db: Session = SessionLocal()

    try:
        # Calculate cutoff time (30 minutes ago)
        cutoff_time = datetime.utcnow() - timedelta(minutes=30)

        # Find stale active conversations
        stale_conversations = (
            db.query(ConversationSession)
            .filter(
                ConversationSession.status == SessionStatus.ACTIVE,
                ConversationSession.last_message_at < cutoff_time,
                ConversationSession.agent_id.is_(None),  # Not agent-handled
            )
            .all()
        )

        abandoned_count = 0
        for session in stale_conversations:
            session.status = SessionStatus.ABANDONED
            abandoned_count += 1
            logger.info(
                f"Auto-abandoned conversation {session.id} "
                f"(inactive for 30+ minutes, last activity: {session.last_message_at})"
            )

        db.commit()

        logger.success(
            f"Auto-abandon task completed: {abandoned_count} conversations marked as ABANDONED"
        )

        return {
            "success": True,
            "abandoned_count": abandoned_count,
            "cutoff_time": cutoff_time.isoformat(),
        }

    except Exception as e:
        logger.error(f"Error in auto_abandon_inactive_conversations: {e}", exc_info=True)
        db.rollback()
        return {"success": False, "error": str(e)}

    finally:
        db.close()


async def auto_resolve_stale_tickets(ctx: dict) -> Dict[str, Any]:
    """
    Auto-resolve tickets with no activity for 7+ days.

    Runs daily at 2 AM.
    Auto-resolves tickets if:
    - Status is OPEN or IN_PROGRESS
    - No updates for 7+ days
    - Also marks associated conversation session as RESOLVED

    Args:
        ctx: ARQ context

    Returns:
        dict with success status and resolved count
    """
    db: Session = SessionLocal()

    try:
        # Calculate cutoff (7 days ago)
        cutoff_time = datetime.utcnow() - timedelta(days=7)

        # Find stale tickets
        stale_tickets = (
            db.query(Ticket)
            .filter(
                Ticket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS]),
                Ticket.updated_at < cutoff_time,  # No updates for 7+ days
            )
            .all()
        )

        resolved_count = 0
        for ticket in stale_tickets:
            # Resolve ticket
            ticket.status = TicketStatus.RESOLVED
            ticket.resolution_notes = (
                "Auto-resolved due to inactivity (7+ days with no updates). "
                "This ticket was automatically closed by the system."
            )
            ticket.resolved_at = datetime.utcnow()

            # Resolve conversation session
            if ticket.session_id:
                session = (
                    db.query(ConversationSession)
                    .filter(ConversationSession.id == ticket.session_id)
                    .first()
                )

                if session:
                    session.status = SessionStatus.RESOLVED

            resolved_count += 1
            logger.info(
                f"Auto-resolved stale ticket {ticket.ticket_number} "
                f"(7+ days inactive, last update: {ticket.updated_at})"
            )

            # Send notification to admins
            try:
                await send_auto_resolve_notification(ticket, db)
            except Exception as e:
                logger.error(
                    f"Failed to send notification for ticket {ticket.ticket_number}: {e}"
                )
                # Continue with other tickets even if notification fails

        db.commit()

        logger.success(
            f"Auto-resolve task completed: {resolved_count} tickets auto-resolved"
        )

        return {
            "success": True,
            "resolved_count": resolved_count,
            "cutoff_time": cutoff_time.isoformat(),
        }

    except Exception as e:
        logger.error(f"Error in auto_resolve_stale_tickets: {e}", exc_info=True)
        db.rollback()
        return {"success": False, "error": str(e)}

    finally:
        db.close()


async def send_auto_resolve_notification(ticket: Ticket, db: Session) -> None:
    """
    Send email notification to admins about auto-resolved ticket.

    Args:
        ticket: The auto-resolved ticket
        db: Database session
    """
    try:
        from app.core.config import settings
        from app.services.tenant_service import TenantService

        # Only send if email is configured
        if not settings.SMTP_HOST or not settings.EMAILS_FROM_EMAIL:
            logger.debug("Email not configured, skipping auto-resolve notification")
            return

        # Get tenant admins and support agents
        admins = (
            db.query(User)
            .filter(
                User.tenant_id == ticket.tenant_id,
                User.role.in_([UserRole.TENANT_ADMIN, UserRole.SUPPORT_AGENT]),
                User.is_active == True,
            )
            .all()
        )

        if not admins:
            logger.warning(
                f"No admins found for tenant {ticket.tenant_id}, skipping notification"
            )
            return

        # Get tenant info
        tenant_service = TenantService(db)
        tenant = tenant_service.get_tenant_by_id(
            ticket.tenant_id, include_relations=False
        )
        tenant_name = tenant.display_name if tenant else "Unknown"

        # Send email to each admin
        from app.utils.email import send_email

        for admin in admins:
            try:
                subject = f"⏰ Ticket Auto-Resolved: {ticket.ticket_number}"

                body = f"""
Hello {admin.full_name},

A ticket has been automatically resolved due to inactivity.

📋 Ticket Details:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Ticket Number: {ticket.ticket_number}
• Tenant: {tenant_name}
• Summary: {ticket.summary}
• Priority: {ticket.priority.value.upper()}
• Created: {ticket.created_at.strftime('%Y-%m-%d %H:%M UTC')}
• Last Updated: {ticket.updated_at.strftime('%Y-%m-%d %H:%M UTC')}
• Auto-Resolved: {ticket.resolved_at.strftime('%Y-%m-%d %H:%M UTC')}

📝 Reason:
This ticket was automatically resolved because there has been no activity
for more than 7 days. The system assumes the issue has been resolved or
is no longer relevant.

💡 What to do:
• If this ticket still needs attention, please manually reopen it
• Review the conversation history to ensure nothing was missed
• Consider following up with the customer if needed

You can view the ticket details in your dashboard.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This is an automated message from Luran Support System.
                """

                await send_email(
                    email_to=admin.email,
                    subject=subject,
                    body=body.strip(),
                )

                logger.info(
                    f"Sent auto-resolve notification to {admin.email} for ticket {ticket.ticket_number}"
                )

            except Exception as e:
                logger.error(
                    f"Failed to send email to {admin.email} for ticket {ticket.ticket_number}: {e}"
                )
                # Continue with other admins even if one fails

    except Exception as e:
        logger.error(f"Failed to send auto-resolve notification: {e}", exc_info=True)
        # Don't raise - this is a non-critical feature
