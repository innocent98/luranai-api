"""Email service for sending transactional emails."""

import re
import smtplib
import ssl
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from app.core.config import settings

# Socket timeout for every SMTP connection: some sends run right after a commit inside a request
# (payment webhook, payout approval), and a dead mail server must not hang that request. 30 s
# leaves room for slow handshakes and quote-PDF attachments, which share this code path.
SMTP_TIMEOUT_SECONDS = 30


_ADDRESS = re.compile(r"[^\s<>,;\"']+@[^\s<>,;\"']+")


def header_text(value: str) -> str:
    """One line, no control characters: a value that came from a visitor or a tenant must never
    be able to end a header and start another."""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())


class EmailService:
    """Service for sending transactional emails with HTML templates."""

    def __init__(self) -> None:
        """Initialize email service with Jinja2 template engine."""
        # Setup Jinja2 for email templates
        template_dir = Path(__file__).parent.parent / "templates" / "emails"
        template_dir.mkdir(parents=True, exist_ok=True)

        self.jinja_env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def _send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: str | None = None,
        attachments: list[tuple[str, bytes, str]] | None = None,
        reply_to: str | None = None,
        from_name: str | None = None,
    ) -> bool:
        """
        Send an email using SMTP.

        Args:
            to_email: Recipient email address
            subject: Email subject
            html_content: HTML content of the email
            text_content: Plain text fallback (optional)
            attachments: Optional list of (filename, content_bytes, mime_type) tuples
            reply_to: Optional Reply-To address (the From address stays the platform's)
            from_name: Optional sender display name; the From address stays the platform's

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            sender = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
            if from_name and header_text(from_name):
                sender = formataddr((header_text(from_name), str(settings.EMAILS_FROM_EMAIL)))
            found = _ADDRESS.search(header_text(reply_to)) if reply_to else None
            reply_address = found.group(0) if found else ""

            # Build message — mixed (with attachments) or alternative (plain)
            if attachments:
                msg = MIMEMultipart("mixed")
                msg["From"] = sender
                msg["To"] = to_email
                msg["Subject"] = subject
                body = MIMEMultipart("alternative")
                if text_content:
                    body.attach(MIMEText(text_content, "plain"))
                body.attach(MIMEText(html_content, "html"))
                msg.attach(body)
                for filename, content, mime in attachments:
                    subtype = mime.split("/", 1)[1] if "/" in mime else "octet-stream"
                    part = MIMEApplication(content, _subtype=subtype)
                    part.add_header("Content-Disposition", "attachment", filename=filename)
                    msg.attach(part)
            else:
                msg = MIMEMultipart("alternative")
                msg["From"] = sender
                msg["To"] = to_email
                msg["Subject"] = subject
                if text_content:
                    msg.attach(MIMEText(text_content, "plain"))
                msg.attach(MIMEText(html_content, "html"))
            if reply_address:
                msg["Reply-To"] = reply_address

            # Send email based on configuration
            # Note: Port 465 uses SSL (implicit), port 587 uses STARTTLS
            if settings.SMTP_PORT == 465:
                # Use SSL (implicit TLS)
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(
                    settings.SMTP_HOST or "",
                    settings.SMTP_PORT,
                    context=context,
                    timeout=SMTP_TIMEOUT_SECONDS,
                ) as server:
                    if settings.SMTP_USER and settings.SMTP_PASSWORD:
                        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                    server.send_message(msg)
            else:
                # Use STARTTLS (port 587)
                with smtplib.SMTP(
                    settings.SMTP_HOST or "", settings.SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS
                ) as server:
                    if settings.SMTP_TLS:
                        context = ssl.create_default_context()
                        server.starttls(context=context)
                    if settings.SMTP_USER and settings.SMTP_PASSWORD:
                        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                    server.send_message(msg)

            logger.info("Email sent successfully")
            return True

        except Exception:
            logger.error("Failed to send email")
            return False

    def render_template(self, template_name: str, **context: Any) -> str:
        """Render a Jinja email template."""
        template = self.jinja_env.get_template(template_name)
        return template.render(**context)

    def send_templated_email(
        self,
        to_email: str,
        subject: str,
        template_name: str,
        context: dict[str, Any],
        text_content: str | None = None,
        reply_to: str | None = None,
        from_name: str | None = None,
    ) -> bool:
        """Render and send a templated email."""
        html_content = self.render_template(template_name, **context)
        return self._send_email(
            to_email=to_email,
            subject=subject,
            html_content=html_content,
            text_content=text_content,
            **self._sender_kwargs(reply_to, from_name),
        )

    @staticmethod
    def _sender_kwargs(reply_to: str | None, from_name: str | None) -> dict[str, str]:
        """Only pass the optional sender fields when set, so existing callers see no change."""
        return {k: v for k, v in (("reply_to", reply_to), ("from_name", from_name)) if v}

    def send_templated_email_with_attachments(
        self,
        to_email: str,
        subject: str,
        template_name: str,
        context: dict[str, Any],
        attachments: list[tuple[str, bytes, str]] | None = None,
        text_content: str | None = None,
        reply_to: str | None = None,
        from_name: str | None = None,
    ) -> bool:
        """Render and send a templated email with optional binary attachments."""
        html_content = self.render_template(template_name, **context)
        return self._send_email(
            to_email=to_email,
            subject=subject,
            html_content=html_content,
            text_content=text_content,
            attachments=attachments,
            **self._sender_kwargs(reply_to, from_name),
        )

    def send_platform_broadcast_email(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        template_key: str,
        subject: str,
        heading: str,
        body_html: str,
        preheader: str | None = None,
        cta_label: str | None = None,
        cta_url: str | None = None,
    ) -> bool:
        """Send a platform-admin broadcast email."""
        template_meta = {
            "announcement": {
                "banner_label": "Platform Announcement",
                "accent_color": "#1d4ed8",
            },
            "feature_update": {
                "banner_label": "Feature Update",
                "accent_color": "#0f766e",
            },
            "general": {
                "banner_label": "Message from Luran",
                "accent_color": "#7c3aed",
            },
            "maintenance": {
                "banner_label": "Maintenance Notice",
                "accent_color": "#c2410c",
            },
            "security": {
                "banner_label": "Security Update",
                "accent_color": "#b91c1c",
            },
            "billing": {
                "banner_label": "Billing Update",
                "accent_color": "#7c2d12",
            },
            "onboarding": {
                "banner_label": "Getting Started",
                "accent_color": "#2563eb",
            },
        }.get(
            template_key,
            {
                "banner_label": "Message from Luran",
                "accent_color": "#1d4ed8",
            },
        )

        return self.send_templated_email(
            to_email=recipient_email,
            subject=subject,
            template_name="platform_broadcast.html",
            context={
                "recipient_name": recipient_name,
                "subject": subject,
                "preheader": preheader,
                "heading": heading,
                "body_html": body_html,
                "cta_label": cta_label,
                "cta_url": cta_url,
                "current_year": datetime.now().year,
                **template_meta,
            },
        )

    def send_channel_update_notification(
        self,
        recipient_email: str,
        recipient_name: str,
        business_name: str,
        post_title: str,
        post_excerpt: str,
        post_url: str,
    ) -> bool:
        """Send a channel update notification email to a follower."""
        try:
            from datetime import datetime

            template = self.jinja_env.get_template("channel_update.html")
            html_content = template.render(
                business_name=business_name,
                post_title=post_title,
                post_excerpt=post_excerpt,
                post_url=post_url,
                current_year=datetime.now().year,
            )

            subject = f"New update from {business_name}"
            return self._send_email(recipient_email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send channel update notification to {recipient_email}: {e}")
            return False

    def send_welcome_email(
        self,
        company_name: str,
        admin_email: str,
        admin_name: str,
        temp_password: str,
        verification_token: str | None = None,
    ) -> bool:
        """
        Send welcome email to new company admin.

        Args:
            company_name: Name of the company
            admin_email: Admin's email address
            admin_name: Admin's full name
            temp_password: Temporary password
            verification_token: Email verification token (optional)

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("welcome.html")
            html_content = template.render(
                company_name=company_name,
                admin_name=admin_name,
                admin_email=admin_email,
                temp_password=temp_password,
                verification_token=verification_token,
                app_url=settings.FRONTEND_URL,
                doc_url=settings.FRONTEND_DOC_URL,
                current_year=datetime.now().year,
            )

            subject = f"Welcome to Luran! 🎉 - {company_name}"
            return self._send_email(admin_email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send welcome email: {e}")
            return False

    def send_user_invite_email(
        self,
        user_email: str,
        user_name: str,
        company_name: str,
        role: str,
        temp_password: str,
        invited_by: str,
    ) -> bool:
        """
        Send invite email to new team member.

        Args:
            user_email: New user's email
            user_name: New user's full name
            company_name: Company name
            role: User's role (SUPPORT_AGENT or VIEWER)
            temp_password: Temporary password
            invited_by: Who invited them (name)

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("user_invite.html")
            html_content = template.render(
                user_name=user_name,
                user_email=user_email,
                company_name=company_name,
                role=role.replace("_", " ").title(),
                temp_password=temp_password,
                invited_by=invited_by,
                app_url=settings.FRONTEND_URL,
                current_year=datetime.now().year,
            )

            subject = f"You've been invited to {company_name} on Luran"
            return self._send_email(user_email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send invite email: {e}")
            return False

    def send_password_reset_email(
        self,
        email: str,
        user_name: str,
        reset_token: str,
        frontend_url: str | None = None,
    ) -> bool:
        """
        Send password reset email.

        Args:
            email: User's email
            user_name: User's full name
            reset_token: Password reset token

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("password_reset.html")
            app_url = frontend_url or settings.FRONTEND_URL
            reset_link = f"{app_url}/reset-password?token={reset_token}"

            html_content = template.render(
                user_name=user_name,
                reset_link=reset_link,
                current_year=datetime.now().year,
            )

            subject = "Reset your Luran password"
            return self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send password reset email: {e}")
            return False

    def send_marketer_invite_email(
        self,
        marketer_email: str,
        marketer_name: str,
        code: str,
        invite_token: str,
        expiry_days: int = 7,
    ) -> bool:
        """
        Send a referral-program marketer their one-time set-password link.

        Args:
            marketer_email: Marketer's email
            marketer_name: Marketer's name
            code: The marketer's referral code
            invite_token: One-time invite token (see TokenService.create_marketer_invite_token)
            expiry_days: Days until the link expires (shown in the email)

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("marketer_invite.html")
            set_password_link = (
                f"{settings.FRONTEND_URL}/marketer/set-password?token={invite_token}"
            )

            html_content = template.render(
                marketer_name=marketer_name,
                code=code,
                set_password_link=set_password_link,
                expiry_days=expiry_days,
                current_year=datetime.now().year,
            )

            subject = "Set up your Luran referral partner account"
            return self._send_email(marketer_email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send marketer invite email: {type(e).__name__}")
            return False

    def send_marketer_password_reset_email(
        self,
        marketer_email: str,
        marketer_name: str,
        reset_token: str,
        expiry_minutes: int = 60,
    ) -> bool:
        """
        Send a referral-program marketer a one-time password-reset link.

        Args:
            marketer_email: Marketer's email
            marketer_name: Marketer's name
            reset_token: One-time token (see TokenService.create_marketer_invite_token)
            expiry_minutes: Minutes until the link expires (shown in the email)

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("marketer_password_reset.html")
            reset_link = f"{settings.FRONTEND_URL}/marketer/set-password?token={reset_token}"

            html_content = template.render(
                marketer_name=marketer_name,
                reset_link=reset_link,
                expiry_minutes=expiry_minutes,
                current_year=datetime.now().year,
            )

            subject = "Reset your Luran referral partner password"
            return self._send_email(marketer_email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send marketer password reset email: {type(e).__name__}")
            return False

    @staticmethod
    def _naira(amount_kobo: int) -> str:
        """Format kobo as naira with thousands separators, e.g. 500000 -> '5,000.00'."""
        return f"{amount_kobo / 100:,.2f}"

    def send_referral_conversion_email(
        self,
        marketer_email: str,
        marketer_name: str,
        amount_kobo: int,
        company_name: str | None = None,
    ) -> bool:
        """
        Tell a marketer that a business they referred became a paying customer.

        The same generic notice goes out whether the reward is pending or held for review: it
        names the reward amount (and the company, which the marketer may know) and nothing else.
        It never carries the customer's email, plan, payment details or any review wording.

        Args:
            marketer_email: Marketer's email
            marketer_name: Marketer's name
            amount_kobo: The reward, in kobo
            company_name: The referred company's display name (optional)

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("referral_conversion.html")
            html_content = template.render(
                marketer_name=marketer_name,
                amount=self._naira(amount_kobo),
                company_name=company_name,
                current_year=datetime.now().year,
            )
            subject = "A business you referred became a paying customer"
            return self._send_email(marketer_email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send referral conversion email: {type(e).__name__}")
            return False

    def send_referral_payout_email(
        self,
        marketer_email: str,
        marketer_name: str,
        amount_kobo: int,
        period_label: str,
        reference: str,
    ) -> bool:
        """
        Tell a marketer that their referral payout has been paid.

        Args:
            marketer_email: Marketer's email
            marketer_name: Marketer's name
            amount_kobo: The amount paid, in kobo
            period_label: The payout batch's period label
            reference: The transfer / bank reference of the payment

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("referral_payout.html")
            html_content = template.render(
                marketer_name=marketer_name,
                amount=self._naira(amount_kobo),
                period_label=period_label,
                reference=reference,
                current_year=datetime.now().year,
            )
            subject = "Your Luran referral payout has been sent"
            return self._send_email(marketer_email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send referral payout email: {type(e).__name__}")
            return False

    def send_password_changed_email(
        self,
        email: str,
        user_name: str,
    ) -> bool:
        """
        Send notification that password was changed.

        Args:
            email: User's email
            user_name: User's full name

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("password_changed.html")
            html_content = template.render(
                user_name=user_name,
                support_email=settings.EMAILS_FROM_EMAIL,
                current_year=datetime.now().year,
            )

            subject = "Your password was changed"
            return self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send password changed email: {e}")
            return False

    def send_whatsapp_capacity_alert_email(
        self,
        to_email: str,
        condition: str,
        detail_lines: list[str],
    ) -> bool:
        """Tell a platform operator WhatsApp capacity is running low. No tenant data."""
        try:
            human = {
                "connections": "connections near the limit",
                "sidecar_memory": "sidecar memory near the limit",
            }.get(condition, condition)
            template = self.jinja_env.get_template("whatsapp_capacity_alert.html")
            html_content = template.render(
                condition_label=human,
                detail_lines=detail_lines,
                current_year=datetime.now().year,
            )
            return self._send_email(to_email, f"[Luran] WhatsApp capacity: {human}", html_content)
        except Exception as e:
            logger.error(f"Failed to send WhatsApp capacity alert email: {type(e).__name__}")
            return False

    def send_verification_email(
        self,
        email: str,
        user_name: str,
        verification_token: str,
        frontend_url: str | None = None,
    ) -> bool:
        """
        Send email verification link.

        Args:
            email: User's email
            user_name: User's full name
            verification_token: Email verification token

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("email_verification.html")
            app_url = frontend_url or settings.FRONTEND_URL
            verification_link = f"{app_url}/verify-email?token={verification_token}"

            html_content = template.render(
                user_name=user_name,
                verification_link=verification_link,
                current_year=datetime.now().year,
            )

            subject = "Verify your email address"
            return self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send verification email: {e}")
            return False

    def send_account_deletion_verification_email(
        self,
        email: str,
        verification_token: str,
        frontend_url: str | None = None,
    ) -> bool:
        """Send the public, unauthenticated account-deletion verification link.

        Deliberately takes no user-identifying fields beyond the recipient
        address itself. The caller (the public deletion-request endpoint)
        must not do an extra lookup just to personalize this email — that
        would reintroduce the exists-vs-doesn't-exist timing difference the
        endpoint is built to avoid.

        Args:
            email: Recipient email (the address the request was made for)
            verification_token: Single-use deletion-verification token
            frontend_url: Optional override of the dashboard base URL

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("account_deletion_verification.html")
            app_url = frontend_url or settings.FRONTEND_URL
            verification_link = f"{app_url}/delete-account/verify?token={verification_token}"

            html_content = template.render(
                verification_link=verification_link,
                current_year=datetime.now().year,
            )

            subject = "Confirm your account deletion request"
            return self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send account deletion verification email: {e}")
            return False

    def send_account_deletion_confirmation_email(
        self,
        email: str,
        cancel_token: str,
        scheduled_purge_at: datetime,
        frontend_url: str | None = None,
    ) -> bool:
        """Send the deletion-scheduled confirmation + cancel link.

        Used for both the authed self-serve/tenant paths and the public
        path once its verification link has been clicked — in every case
        the account is now in the cancellable grace period and this is the
        one link that can restore it.

        Args:
            email: Recipient email
            cancel_token: Single-use grace-period cancel token
            scheduled_purge_at: When the grace period ends (irreversible purge)
            frontend_url: Optional override of the dashboard base URL

        Returns:
            True if sent successfully
        """
        try:
            template = self.jinja_env.get_template("account_deletion_confirmation.html")
            app_url = frontend_url or settings.FRONTEND_URL
            cancel_link = f"{app_url}/delete-account/cancel?token={cancel_token}"

            html_content = template.render(
                cancel_link=cancel_link,
                scheduled_purge_at=scheduled_purge_at.strftime("%B %d, %Y"),
                current_year=datetime.now().year,
            )

            subject = "Your account deletion is scheduled"
            return self._send_email(email, subject, html_content)

        except Exception as e:
            logger.error(f"Failed to send account deletion confirmation email: {e}")
            return False


# Singleton instance
email_service = EmailService()
