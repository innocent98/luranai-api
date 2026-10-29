"""Marketer authentication: login, refresh-token rotation, password set/reset.

A marketer is a separate identity from tenant users and platform admins. Its JWT carries
``admin_type="marketer"`` and deliberately NO ``role``, ``tenant_id`` or ``auth_type`` claim, so
the tenant (needs ``role`` + ``tenant_id``), platform-admin (needs ``admin_type="platform_admin"``)
and customer (needs ``auth_type="customer"``) verifiers each reject it on their own.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption import hash_for_lookup
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
)
from app.db.models.referral import Marketer, MarketerStatus
from app.db.models.refresh_token import RefreshToken
from app.services.email_service import email_service
from app.services.referral.marketer_service import normalise_email
from app.services.token_service import token_service

MARKETER_TOKEN_TYPE = "marketer"
RESET_LINK_MINUTES = 60


class MarketerAuthError(Exception):
    """Base class: ``code`` is the stable machine-readable error string."""

    code = "marketer_auth_error"


class InvalidCredentialsError(MarketerAuthError):
    code = "invalid_credentials"


class AccountSuspendedError(MarketerAuthError):
    code = "account_suspended"


class InvalidRefreshTokenError(MarketerAuthError):
    code = "invalid_refresh_token"


class InvalidTokenError(MarketerAuthError):
    code = "invalid_or_expired_token"


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """A real bcrypt hash (same cost as stored ones) to verify against for unknown emails."""
    return get_password_hash("dummy-password-for-constant-time-checks")


def marketer_id_from_claims(claims: dict) -> UUID | None:
    """The marketer id in a decoded JWT, or None unless it is strictly a marketer token."""
    if claims.get("admin_type") != MARKETER_TOKEN_TYPE:
        return None
    try:
        return UUID(str(claims.get("sub")))
    except (ValueError, TypeError):
        return None


def _hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class MarketerAuthService:
    def __init__(self, db: Session):
        self.db = db

    # --- tokens -------------------------------------------------------------------------------

    def _issue_tokens(self, marketer: Marketer) -> dict:
        access_token = create_access_token(
            subject=str(marketer.id),
            additional_claims={"admin_type": MARKETER_TOKEN_TYPE},
        )
        raw_refresh, refresh_hash = create_refresh_token()
        self.db.add(
            RefreshToken(
                user_id=marketer.id,
                user_type=MARKETER_TOKEN_TYPE,
                token_hash=refresh_hash,
                expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
                created_at=datetime.now(UTC),
            )
        )
        self.db.commit()
        return {
            "access_token": access_token,
            "refresh_token": raw_refresh,
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }

    def revoke_all_refresh_tokens(self, marketer_id: UUID) -> None:
        """Mark every live refresh token of this marketer revoked (caller commits)."""
        records = (
            self.db.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == marketer_id,
                    RefreshToken.user_type == MARKETER_TOKEN_TYPE,
                    RefreshToken.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        for record in records:
            record.revoked_at = now

    # --- login / refresh / logout -------------------------------------------------------------

    def _by_email(self, email: str) -> Marketer | None:
        return self.db.execute(
            select(Marketer).where(Marketer.email_hash == hash_for_lookup(normalise_email(email)))
        ).scalar_one_or_none()

    def login(self, email: str, password: str) -> dict:
        """Verify credentials and issue a token pair.

        Unknown email, no password set yet and wrong password are indistinguishable (same error,
        and a bcrypt verification always runs). Suspension is only revealed to someone who proved
        the correct password.
        """
        marketer = self._by_email(email)
        stored_hash = marketer.password_hash if marketer is not None else None
        password_ok = verify_password(password, stored_hash or _dummy_password_hash())
        if marketer is None or not stored_hash or not password_ok:
            raise InvalidCredentialsError("Incorrect email or password")
        if marketer.status != MarketerStatus.ACTIVE:
            raise AccountSuspendedError("Account suspended")
        return self._issue_tokens(marketer)

    def refresh(self, raw_refresh_token: str) -> dict:
        """Rotate a refresh token. Re-checks the marketer is still active."""
        record = self.db.execute(
            select(RefreshToken)
            .where(
                RefreshToken.token_hash == _hash_refresh(raw_refresh_token),
                RefreshToken.user_type == MARKETER_TOKEN_TYPE,
                RefreshToken.revoked_at.is_(None),
            )
            .with_for_update()  # two concurrent refreshes of one token: only one may win
        ).scalar_one_or_none()
        if record is None or record.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
            raise InvalidRefreshTokenError("Invalid or expired refresh token")

        marketer = self.db.get(Marketer, record.user_id)
        record.revoked_at = datetime.now(UTC)  # rotation: the presented token is spent either way
        if marketer is None or marketer.status != MarketerStatus.ACTIVE:
            self.db.commit()
            raise InvalidRefreshTokenError("Invalid or expired refresh token")
        return self._issue_tokens(marketer)  # commits the revocation with the new record

    def revoke_refresh_token(self, raw_refresh_token: str) -> None:
        """Server-side logout. Only ever touches a marketer's own refresh record."""
        record = self.db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == _hash_refresh(raw_refresh_token),
                RefreshToken.user_type == MARKETER_TOKEN_TYPE,
            )
        ).scalar_one_or_none()
        if record is not None and record.revoked_at is None:
            record.revoked_at = datetime.now(UTC)
            self.db.commit()

    # --- set / reset password -----------------------------------------------------------------

    def set_password(self, token: str, new_password: str) -> None:
        """Redeem an invite or reset token (one-time) and set the password.

        Every failure is the same ``InvalidTokenError``: bad/used/expired token, marketer gone,
        suspended, or the token was issued for an email the marketer no longer has.
        """
        data = token_service.verify_marketer_invite_token(token)
        if not data:
            raise InvalidTokenError("Invalid or expired token")

        marketer = self.db.execute(
            select(Marketer).where(Marketer.id == data["marketer_id"]).with_for_update()
        ).scalar_one_or_none()
        if (
            marketer is None
            or marketer.status != MarketerStatus.ACTIVE
            or normalise_email(marketer.email) != normalise_email(str(data["email"]))
        ):
            raise InvalidTokenError("Invalid or expired token")

        marketer.password_hash = get_password_hash(new_password)
        self.revoke_all_refresh_tokens(marketer.id)
        self.db.commit()

    def reset_target(self, email: str) -> tuple[UUID, str, str] | None:
        """(id, email, name) of the ACTIVE marketer behind this address, else None."""
        marketer = self._by_email(email)
        if marketer is None or marketer.status != MarketerStatus.ACTIVE:
            return None
        return marketer.id, marketer.email, marketer.name


def send_password_reset(marketer_id: UUID, email: str, name: str) -> None:
    """Create a one-time reset token and email it. Runs after the response; never raises."""
    try:
        token = token_service.create_marketer_invite_token(
            marketer_id, email, expiry_minutes=RESET_LINK_MINUTES
        )
        sent = email_service.send_marketer_password_reset_email(
            marketer_email=email,
            marketer_name=name,
            reset_token=token,
            expiry_minutes=RESET_LINK_MINUTES,
        )
        if not sent:
            logger.error(f"Marketer password reset email was not sent for marketer {marketer_id}")
    except Exception as exc:
        logger.error(f"Failed to send marketer password reset for marketer {marketer_id}: {exc}")
