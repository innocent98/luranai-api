"""Account-deletion request lifecycle: request / verify / cancel + last-admin guard.

Owns the ``active -> pending_verification -> grace -> (cancelled|purged)`` state
machine for ``DeletionRequest`` rows (see ``app/db/models/deletion_request.py``)
and mirrors the current stage onto ``User.deletion_status`` /
``User.deletion_requested_at``. This service never touches PII or performs the
irreversible purge itself — that is ``AccountPurgeService`` (a later task). It
only decides *whether* a request may proceed, tracks its lifecycle, and
disables/re-enables login access around it.

Session revocation: this codebase's durable session store is ``RefreshToken``
rows (access tokens are short-lived, stateless JWTs with no server-side
registry of every live ``jti``, so they cannot be enumerated and blacklisted
here). Disabling a user therefore revokes all of their active refresh tokens
(mirrors ``AuthService._revoke_all_user_tokens``) so no *new* access token can
be minted. Callers that already hold a live access token for this request
(e.g. the authed self-serve endpoint) should additionally pass its ``jti`` via
``current_jti`` so it is blacklisted immediately through the existing
``revoked:{jti}`` mechanism (``app/api/deps.py``), the same way ``logout()``
does — without it, that one still-valid token remains usable until it
naturally expires.
"""

import hashlib
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import revoke_token
from app.core.config import settings
from app.core.encryption import hash_for_lookup
from app.core.security import create_refresh_token
from app.db.models.deletion_request import DeletionRequest, DeletionRequestedVia, DeletionScope
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import DeletionStatus, User, UserRole
from app.utils.datetime import ensure_utc_aware, utcnow_aware

# A request is "open" while it is actively working towards a purge — i.e. it
# has not been cancelled (back to active) and not yet purged.
_OPEN_STATUSES = (DeletionStatus.PENDING_VERIFICATION, DeletionStatus.GRACE)


class AccountDeletionError(Exception):
    """Base error for the deletion-request lifecycle."""


class LastAdminError(AccountDeletionError):
    """Raised when a ``tenant_admin`` is the sole remaining active admin.

    Carries machine-readable ``options`` for the caller (the API layer) to
    surface: transfer ownership to another member first, or explicitly
    delete the whole tenant via ``request_tenant``.
    """

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.options: list[str] = ["transfer_ownership", "delete_tenant"]
        super().__init__(
            "Cannot delete the sole remaining tenant admin for this tenant; "
            "transfer ownership to another member or delete the whole tenant instead."
        )


class InvalidOrExpiredTokenError(AccountDeletionError):
    """Raised when a verification/cancel token is unknown, stale, or expired."""

    def __init__(self, message: str = "Invalid or expired token.") -> None:
        super().__init__(message)


class AlreadyPurgedError(AccountDeletionError):
    """Raised on cancel when the target request has already been purged."""

    def __init__(self, message: str = "This account has already been purged.") -> None:
        super().__init__(message)


class NotTenantAdminError(AccountDeletionError):
    """Raised when a non-admin calls the tenant-wide deletion path."""

    def __init__(self, message: str = "Tenant admin access required.") -> None:
        super().__init__(message)


class AccountDeletionService:
    """Service for AccountDeletion request/verify/cancel lifecycle."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_self(self, user: User, *, current_jti: str | None = None) -> DeletionRequest:
        """Authed self-serve deletion request (identity already proven by the session).

        Enforces the last-admin guard for ``tenant_admin`` callers. Idempotent:
        reuses an existing open request for this user rather than creating a
        second one. The raw, one-time cancel token is attached to the
        returned row as ``.raw_cancel_token`` (not persisted) for the caller
        to email.
        """
        self._guard_last_admin(user)

        existing = self._get_open_request(user.id)
        if existing is not None:
            if existing.status == DeletionStatus.GRACE:
                # Already in grace — reissue a fresh cancel token so the
                # caller always has something actionable to (re-)email,
                # without resetting the purge clock.
                raw_cancel_token, existing.cancel_token_hash = create_refresh_token()
            else:
                # Had a public pending_verification request in flight;
                # identity is now proven via the active session, so promote
                # it straight to grace instead of leaving it dangling.
                raw_cancel_token = self._enter_grace(existing, user, current_jti=current_jti)
            self.db.commit()
            self.db.refresh(existing)
            existing.raw_cancel_token = raw_cancel_token  # type: ignore[attr-defined]
            return existing

        request = DeletionRequest(
            user_id=user.id,
            tenant_id=user.tenant_id,
            scope=DeletionScope.SELF_USER,
            requested_via=DeletionRequestedVia.AUTHED,
        )
        self.db.add(request)
        self.db.flush()

        raw_cancel_token = self._enter_grace(request, user, current_jti=current_jti)
        self.db.commit()
        self.db.refresh(request)
        request.raw_cancel_token = raw_cancel_token  # type: ignore[attr-defined]
        return request

    def request_public(self, email: str) -> str | None:
        """Public, unauthenticated deletion request resolved by email.

        Returns the raw verification token when a matching, deletable
        account is found, else ``None``. **Callers must not let the return
        value (or its presence) leak into the HTTP response** — the public
        endpoint contract requires a byte-identical response regardless of
        whether the email exists (anti-enumeration). This method only
        decides whether to create/refresh a request; it never sends email or
        shapes a response. The last-admin guard is intentionally NOT applied
        here (this caller's role can't be trusted as identity-proven yet) —
        it is re-applied in ``verify()`` once the link has been clicked.
        """
        user = (
            self.db.execute(
                select(User).where(User.email_hash == hash_for_lookup(email.strip().lower()))
            )
            .scalars()
            .first()
        )

        if user is None or user.deletion_status == DeletionStatus.PURGED:
            return None

        existing = self._get_open_request(user.id)
        if existing is not None:
            if existing.status != DeletionStatus.PENDING_VERIFICATION:
                # Already grace (verified) or otherwise in flight — nothing
                # new to email via the public re-request path.
                return None
            raw_token, existing.verification_token_hash = create_refresh_token()
            self.db.commit()
            return raw_token

        raw_token, verification_token_hash = create_refresh_token()
        request = DeletionRequest(
            user_id=user.id,
            tenant_id=user.tenant_id,
            scope=DeletionScope.SELF_USER,
            status=DeletionStatus.PENDING_VERIFICATION,
            requested_via=DeletionRequestedVia.PUBLIC,
            verification_token_hash=verification_token_hash,
        )
        self.db.add(request)
        user.deletion_requested_at = utcnow_aware()
        self.db.commit()
        return raw_token

    def verify(self, token: str, *, current_jti: str | None = None) -> DeletionRequest:
        """Move a public request ``pending_verification -> grace``.

        Disables the account and revokes sessions (identity is now proven by
        possession of the emailed token). Re-applies the last-admin guard —
        the caller's tenant-admin status may have changed since the request
        was created. Tokens are single-use: the verification hash is cleared
        on success. The raw, one-time cancel token is attached to the
        returned row as ``.raw_cancel_token`` for the caller to email.
        """
        token_hash = self._hash_token(token)
        request = (
            self.db.execute(
                select(DeletionRequest).where(DeletionRequest.verification_token_hash == token_hash)
            )
            .scalars()
            .first()
        )

        if request is None or request.status != DeletionStatus.PENDING_VERIFICATION:
            raise InvalidOrExpiredTokenError("Invalid or expired verification token.")

        if self._verification_token_expired(request):
            raise InvalidOrExpiredTokenError("Invalid or expired verification token.")

        user = self.db.get(User, request.user_id)
        if user is None:
            raise InvalidOrExpiredTokenError("Invalid or expired verification token.")

        self._guard_last_admin(user)

        raw_cancel_token = self._enter_grace(request, user, current_jti=current_jti)
        self.db.commit()
        self.db.refresh(request)
        request.raw_cancel_token = raw_cancel_token  # type: ignore[attr-defined]
        return request

    def cancel(self, token_or_user: str | User) -> None:
        """Restore an account still in grace (``grace -> active``).

        Accepts either the raw cancel token (public email link) or the
        authenticated ``User`` (authed "cancel pending deletion"). Raises
        ``AlreadyPurgedError`` if the request has already been purged, or
        ``InvalidOrExpiredTokenError`` if there is nothing cancellable.
        """
        request = self._resolve_for_cancel(token_or_user)
        if request is None:
            raise InvalidOrExpiredTokenError("Invalid or expired cancellation token.")

        if request.status == DeletionStatus.PURGED:
            raise AlreadyPurgedError()

        if request.status not in _OPEN_STATUSES:
            raise InvalidOrExpiredTokenError("Invalid or expired cancellation token.")

        user = self.db.get(User, request.user_id)

        request.status = DeletionStatus.ACTIVE
        request.scheduled_purge_at = None
        request.verification_token_hash = None
        request.cancel_token_hash = None

        if user is not None:
            user.deletion_status = DeletionStatus.ACTIVE
            user.deletion_requested_at = None

        self.db.commit()

    def request_tenant(self, admin: User) -> DeletionRequest:
        """The last-admin explicit whole-tenant deletion path (``tenant_admin`` only).

        Does not go through the last-admin guard — this IS the path that
        exists specifically for the sole remaining admin. Creates (or reuses
        an existing open) tenant-scoped request. Disables and revokes
        sessions for the requesting admin; the actual member-user cascade is
        applied at purge time (a later task), not here.
        """
        if admin.role != UserRole.TENANT_ADMIN:
            raise NotTenantAdminError()

        existing = (
            self.db.execute(
                select(DeletionRequest)
                .where(
                    DeletionRequest.tenant_id == admin.tenant_id,
                    DeletionRequest.scope == DeletionScope.TENANT,
                    DeletionRequest.status.in_(_OPEN_STATUSES),
                )
                .order_by(DeletionRequest.created_at.desc())
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return existing

        request = DeletionRequest(
            user_id=admin.id,
            tenant_id=admin.tenant_id,
            scope=DeletionScope.TENANT,
            requested_via=DeletionRequestedVia.AUTHED,
        )
        self.db.add(request)
        self.db.flush()

        raw_cancel_token = self._enter_grace(request, admin, current_jti=None)
        self.db.commit()
        self.db.refresh(request)
        request.raw_cancel_token = raw_cancel_token  # type: ignore[attr-defined]
        return request

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _guard_last_admin(self, user: User) -> None:
        """Block self-deletion if ``user`` is the sole remaining active tenant_admin."""
        if user.role != UserRole.TENANT_ADMIN:
            return

        other_active_admins = self.db.execute(
            select(func.count(User.id)).where(
                User.tenant_id == user.tenant_id,
                User.role == UserRole.TENANT_ADMIN,
                User.id != user.id,
                User.deletion_status == DeletionStatus.ACTIVE,
            )
        ).scalar_one()

        if other_active_admins == 0:
            raise LastAdminError(tenant_id=user.tenant_id)

    def _get_open_request(self, user_id: UUID) -> DeletionRequest | None:
        return (
            self.db.execute(
                select(DeletionRequest)
                .where(
                    DeletionRequest.user_id == user_id, DeletionRequest.status.in_(_OPEN_STATUSES)
                )
                .order_by(DeletionRequest.created_at.desc())
            )
            .scalars()
            .first()
        )

    def _resolve_for_cancel(self, token_or_user: str | User) -> DeletionRequest | None:
        if isinstance(token_or_user, User):
            return (
                self.db.execute(
                    select(DeletionRequest)
                    .where(DeletionRequest.user_id == token_or_user.id)
                    .order_by(DeletionRequest.created_at.desc())
                )
                .scalars()
                .first()
            )

        token_hash = self._hash_token(token_or_user)
        return (
            self.db.execute(
                select(DeletionRequest).where(DeletionRequest.cancel_token_hash == token_hash)
            )
            .scalars()
            .first()
        )

    def _enter_grace(self, request: DeletionRequest, user: User, *, current_jti: str | None) -> str:
        """Shared ``-> grace`` transition: schedule purge, disable, revoke sessions.

        Returns the raw (unhashed) cancel token for the caller to email.
        """
        now = utcnow_aware()
        request.status = DeletionStatus.GRACE
        request.scheduled_purge_at = now + timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)
        request.verification_token_hash = None
        raw_cancel_token, request.cancel_token_hash = create_refresh_token()

        user.deletion_status = DeletionStatus.GRACE
        user.deletion_requested_at = now

        self._revoke_sessions(user.id, current_jti=current_jti)
        return raw_cancel_token

    def _revoke_sessions(self, user_id: UUID, *, current_jti: str | None = None) -> None:
        """Revoke this user's persisted sessions (refresh tokens) + the calling jti, if known."""
        now = utcnow_aware()
        records = (
            self.db.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.user_type == "tenant_user",
                    RefreshToken.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        for record in records:
            record.revoked_at = now

        if current_jti:
            revoke_token(current_jti, settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)

    def _verification_token_expired(self, request: DeletionRequest) -> bool:
        created_at = ensure_utc_aware(request.created_at)
        assert created_at is not None  # created_at is non-nullable
        ttl = timedelta(hours=settings.ACCOUNT_DELETION_VERIFICATION_TOKEN_TTL_HOURS)
        return created_at + ttl < utcnow_aware()

    @staticmethod
    def _hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()
