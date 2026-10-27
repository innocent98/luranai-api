"""WhatsApp connection management (tenant-facing): create, pair, list, patch, delete.

Every query is scoped by `tenant_id`; an unknown id and another tenant's id are both
`not_found` (404), never 403. The pairing code is handed to the caller once and is never
stored, logged or put in an exception message.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from dataclasses import dataclass
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels import identity
from app.channels.base import (
    CapacityExceeded,
    NotPairable,
    PairingResult,
    SessionExists,
    SessionNotFound,
    SidecarUnavailable,
)
from app.channels.registry import get_adapter
from app.core.config import settings
from app.db.models.tenant import TenantCapabilities
from app.db.models.whatsapp import (
    LIVE_STATUSES,
    WhatsAppChat,
    WhatsAppConnection,
    WhatsAppInboundMessage,
    WhatsAppProvider,
    WhatsAppStatus,
)
from app.utils.datetime import utcnow_aware
from app.utils.http_rate_limiter import (
    enforce_identifier_limit,
    whatsapp_create_rate_limiter,
    whatsapp_pairing_conn_rate_limiter,
    whatsapp_phone_rate_limiter,
)

# DRAFT: wording is NOT approved. Adebayo must approve the final text before any real
# tenant sees it; bump CONSENT_VERSION whenever the text changes (old versions are rejected).
CONSENT_VERSION = "2026-10-27-draft1"
CONSENT_TEXT = (
    "DRAFT - wording pending approval.\n"
    "Luran connects your WhatsApp number as a linked device (like WhatsApp Web), which is not "
    "an official WhatsApp integration. WhatsApp may restrict or ban a number that is used this "
    "way, and Luran is not liable for that or for any resulting loss of the number. We "
    "recommend an aged business number that you use only for business. "
    "Luran only replies to customers who message you first. You can keep using WhatsApp on "
    "your phone or WhatsApp Web at the same time: when you type a reply yourself, the AI "
    "pauses on that chat for a while. You can disconnect at any time."
)


# An unpaired connection older than this is discarded when the next one is created.
STALE_PAIRING_AFTER = timedelta(minutes=30)  # absolute, from created_at: new codes never extend it


class WhatsAppError(Exception):
    """Mapped to an HTTP response `{code, message}` by the registered handler."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _not_found() -> WhatsAppError:
    return WhatsAppError(404, "not_found", "WhatsApp connection not found")


@dataclass
class CreatedConnection:
    connection: WhatsAppConnection
    pairing: PairingResult


def connection_to_dict(conn: WhatsAppConnection) -> dict:
    digits = conn.phone_e164
    return {
        "id": str(conn.id),
        "label": conn.label,
        "status": conn.status,
        "phone_masked": identity.mask_phone(digits),
        "ai_enabled": conn.ai_enabled,
        "last_connected_at": conn.last_connected_at,
        "last_disconnect_reason": conn.last_disconnect_reason,
    }


class WhatsAppService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------ lookup
    def get_for_tenant(self, tenant_id: UUID, connection_id: UUID) -> WhatsAppConnection:
        conn = self.db.execute(
            select(WhatsAppConnection).where(
                WhatsAppConnection.id == connection_id,
                WhatsAppConnection.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if conn is None:
            raise _not_found()
        return conn

    def list_for_tenant(self, tenant_id: UUID) -> list[WhatsAppConnection]:
        return list(
            self.db.execute(
                select(WhatsAppConnection)
                .where(WhatsAppConnection.tenant_id == tenant_id)
                .order_by(WhatsAppConnection.created_at)
            ).scalars()
        )

    # ------------------------------------------------------------------ create
    def _require_enabled(self, tenant_id: UUID) -> TenantCapabilities:
        caps = self.db.execute(
            select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if not settings.WHATSAPP_ENABLED or caps is None or not caps.can_use_whatsapp:
            raise WhatsAppError(
                403, "whatsapp_not_enabled", "WhatsApp is not enabled for this account."
            )
        return caps

    def _lock_capacity(self) -> None:
        if self.db.get_bind().dialect.name == "postgresql":
            # Serialise capacity/limit checks + insert across workers (released at commit).
            self.db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 7_300_001})

    async def _expire_stale_pairings(self) -> None:
        """Free numbers/slots held by pairing attempts nobody finished (codes live 3 minutes).

        Without this, anyone registering a number they cannot pair would block it (and one
        of the few global slots) forever.
        """
        cutoff = utcnow_aware() - STALE_PAIRING_AFTER
        stale = list(
            self.db.execute(
                select(WhatsAppConnection).where(
                    WhatsAppConnection.status == WhatsAppStatus.AWAITING_PAIR.value,
                    WhatsAppConnection.last_connected_at.is_(None),
                    WhatsAppConnection.created_at < cutoff,
                )
            ).scalars()
        )
        for conn in stale:
            try:
                await get_adapter(conn.provider).delete_session(conn.session_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stale pairing sidecar cleanup failed: {}", type(exc).__name__)
                continue  # keep the row so the sidecar session is not orphaned
            self.db.delete(conn)
        if stale:
            self.db.commit()
            self._lock_capacity()

    def _live_count(self) -> int:
        return int(
            self.db.execute(
                select(func.count())
                .select_from(WhatsAppConnection)
                .where(WhatsAppConnection.status.in_(LIVE_STATUSES))
            ).scalar_one()
        )

    async def create_connection(
        self,
        tenant_id: UUID,
        user_id: UUID,
        phone_number: str,
        label: str | None,
        consent_version: str | None,
        consent_accepted: bool,
    ) -> CreatedConnection:
        caps = self._require_enabled(tenant_id)

        if not consent_accepted or consent_version != CONSENT_VERSION:
            raise WhatsAppError(
                422,
                "consent_required",
                "Accept the current WhatsApp terms (version " + CONSENT_VERSION + ") to continue.",
            )
        digits = identity.normalize_phone(phone_number)
        if digits is None:
            raise WhatsAppError(
                422,
                "invalid_phone",
                "Enter the number as digits with country code and no '+', e.g. 2348012345678.",
            )
        try:
            phone_key = identity.phone_hmac(digits)
        except identity.PepperMissing:
            logger.error("WHATSAPP_CONTACT_PEPPER is not configured")
            raise WhatsAppError(
                503, "whatsapp_misconfigured", "WhatsApp is temporarily unavailable."
            ) from None

        # Each create is a pairing attempt (WhatsApp notifies the target number's owner):
        # cap it per tenant and per target number across all tenants, not only per IP.
        await enforce_identifier_limit(whatsapp_create_rate_limiter, f"tenant:{tenant_id}")

        self._lock_capacity()
        await self._expire_stale_pairings()

        if self.db.execute(
            select(WhatsAppConnection.id).where(WhatsAppConnection.phone_hmac == phone_key)
        ).first():
            raise WhatsAppError(
                409, "number_in_use", "This number is already connected to a Luran account."
            )
        owned = self.db.execute(
            select(func.count())
            .select_from(WhatsAppConnection)
            .where(WhatsAppConnection.tenant_id == tenant_id)
        ).scalar_one()
        if owned >= caps.max_whatsapp_numbers:
            raise WhatsAppError(
                409,
                "connection_limit",
                f"Your plan allows {caps.max_whatsapp_numbers} WhatsApp number(s).",
            )
        if self._live_count() >= settings.WHATSAPP_MAX_CONNECTIONS:
            raise WhatsAppError(
                503,
                "capacity_exceeded",
                "WhatsApp is at capacity right now. Please try again later.",
            )

        # Only attempts that will really reach the sidecar spend the per-number budget, so
        # probing a number that is already in use cannot lock its real owner out.
        await enforce_identifier_limit(whatsapp_phone_rate_limiter, phone_key)

        conn = WhatsAppConnection(
            tenant_id=tenant_id,
            provider=WhatsAppProvider.LINKED_DEVICE.value,
            status=WhatsAppStatus.AWAITING_PAIR.value,
            label=(label or "").strip() or None,
            phone_e164=digits,
            phone_hmac=phone_key,
            session_key=secrets.token_urlsafe(24),
            consent_version=CONSENT_VERSION,
            consent_accepted_by=user_id,
            consent_accepted_at=utcnow_aware(),
            ai_enabled=True,
        )
        self.db.add(conn)
        try:
            self.db.commit()  # row exists (counts for capacity) before the sidecar is called
        except IntegrityError:
            self.db.rollback()
            raise WhatsAppError(
                409, "number_in_use", "This number is already connected to a Luran account."
            ) from None
        self.db.refresh(conn)

        try:
            pairing = await get_adapter(conn.provider).create_session(conn.session_key, digits)
        except CapacityExceeded:
            self._discard(conn)
            raise WhatsAppError(
                503,
                "capacity_exceeded",
                "WhatsApp is at capacity right now. Please try again later.",
            ) from None
        except (SessionExists, SidecarUnavailable) as exc:
            self._discard(conn)
            logger.warning("whatsapp create_session failed: {}", exc.code)
            raise WhatsAppError(
                502, "sidecar_unavailable", "Could not reach WhatsApp. Please try again."
            ) from None
        return CreatedConnection(connection=conn, pairing=pairing)

    def _discard(self, conn: WhatsAppConnection) -> None:
        self.db.delete(conn)
        self.db.commit()

    # ------------------------------------------------------------------ pairing
    async def new_pairing_code(self, tenant_id: UUID, connection_id: UUID) -> PairingResult:
        self._require_enabled(tenant_id)  # a revoked grant must not keep minting codes
        conn = self.get_for_tenant(tenant_id, connection_id)
        if conn.status not in (WhatsAppStatus.AWAITING_PAIR.value, WhatsAppStatus.LOGGED_OUT.value):
            raise WhatsAppError(
                409, "not_pairable", "This connection can only be paired while it is waiting."
            )
        await enforce_identifier_limit(whatsapp_pairing_conn_rate_limiter, str(conn.id))
        # every code reaches the number's owner, so the per-number budget applies here too
        await enforce_identifier_limit(whatsapp_phone_rate_limiter, conn.phone_hmac)
        if conn.status == WhatsAppStatus.LOGGED_OUT.value:
            # Re-pairing makes the session live again, so the capacity guard applies.
            self._lock_capacity()
            if self._live_count() >= settings.WHATSAPP_MAX_CONNECTIONS:
                raise WhatsAppError(
                    503,
                    "capacity_exceeded",
                    "WhatsApp is at capacity right now. Please try again later.",
                )
        try:
            pairing = await get_adapter(conn.provider).new_pairing_code(conn.session_key)
        except NotPairable:
            raise WhatsAppError(
                409, "not_pairable", "This connection can only be paired while it is waiting."
            ) from None
        except CapacityExceeded:
            raise WhatsAppError(
                503,
                "capacity_exceeded",
                "WhatsApp is at capacity right now. Please try again later.",
            ) from None
        except (SessionNotFound, SidecarUnavailable) as exc:
            logger.warning("whatsapp new_pairing_code failed: {}", exc.code)
            raise WhatsAppError(
                502, "sidecar_unavailable", "Could not reach WhatsApp. Please try again."
            ) from None
        conn.status = WhatsAppStatus.AWAITING_PAIR.value
        self.db.commit()
        return pairing

    # ------------------------------------------------------------------ patch / delete
    def patch(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        *,
        ai_enabled: bool | None,
        label: str | None,
        label_set: bool,
    ) -> WhatsAppConnection:
        conn = self.get_for_tenant(tenant_id, connection_id)
        if ai_enabled is not None:
            conn.ai_enabled = ai_enabled
        if label_set:
            conn.label = (label or "").strip() or None
        self.db.commit()
        self.db.refresh(conn)
        return conn

    async def delete(self, tenant_id: UUID, connection_id: UUID) -> None:
        """Logout + wipe at the sidecar, then drop the row (cascades chats and the ledger).

        Conversations are kept: `conversation_sessions` has no FK to the connection. If the
        sidecar cannot be reached the row is KEPT (fail closed): deleting it would orphan a
        live linked session that nobody can then unlink from the dashboard.
        """
        conn = self.get_for_tenant(tenant_id, connection_id)
        try:
            await get_adapter(conn.provider).delete_session(conn.session_key)
        except SidecarUnavailable as exc:
            logger.warning("whatsapp delete_session failed: {}", exc.code)
            raise WhatsAppError(
                502, "sidecar_unavailable", "Could not reach WhatsApp. Please try again."
            ) from None
        # Explicit (not only FK cascade): contact refs must be wiped on every backend.
        self.db.execute(delete(WhatsAppChat).where(WhatsAppChat.connection_id == conn.id))
        self.db.execute(
            delete(WhatsAppInboundMessage).where(WhatsAppInboundMessage.connection_id == conn.id)
        )
        self.db.delete(conn)
        self.db.commit()


async def logout_tenant_connections(db: Session, tenant_id: UUID) -> int:
    """Best-effort sidecar logout for every connection of a tenant (account purge).

    Never raises: a purge must not be blocked by an unreachable sidecar. Returns how many
    sessions were logged out.
    """
    done = 0  # deliberately NOT gated on the kill switch: the sessions must still be unlinked
    for conn in WhatsAppService(db).list_for_tenant(tenant_id):
        try:
            await get_adapter(conn.provider).delete_session(conn.session_key)
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("whatsapp logout during tenant purge failed: {}", type(exc).__name__)
    return done
