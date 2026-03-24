"""Email service for sending transactional emails."""

import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from app.core.config import settings


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
    ) -> bool:
        """
        Send an email using SMTP.

        Args:
            to_email: Recipient email address
            subject: Email subject
            html_content: HTML content of the email
            text_content: Plain text fallback (optional)

        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Create message
            msg = MIMEMultipart("alternative")
            msg["From"] = f"{settings.EMAILS_FROM_NAME} <{settings.EMAILS_FROM_EMAIL}>"
            msg["To"] = to_email
            msg["Subject"] = subject

            # Attach text and HTML parts
            if text_content:
                msg.attach(MIMEText(text_content, "plain"))
            msg.attach(MIMEText(html_content, "html"))

            # Send email based on configuration
            # Note: Port 465 uses SSL (implicit), port 587 uses STARTTLS
            if settings.SMTP_PORT == 465:
                # Use SSL (implicit TLS)
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(
                    settings.SMTP_HOST or "",
                    settings.SMTP_PORT,
                    context=context,
                ) as server:
                    if settings.SMTP_USER and settings.SMTP_PASSWORD:
                        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                    server.send_message(msg)
            else:
                # Use STARTTLS (port 587)
                with smtplib.SMTP(settings.SMTP_HOST or "", settings.SMTP_PORT) as server:
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
            reset_link = f"{settings.FRONTEND_URL}/reset-password?token={reset_token}"

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

    def send_verification_email(
        self,
        email: str,
        user_name: str,
        verification_token: str,
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
            verification_link = f"{settings.FRONTEND_URL}/verify-email?token={verification_token}"

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


# Singleton instance
email_service = EmailService()
