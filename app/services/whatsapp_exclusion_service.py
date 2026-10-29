"""Tenant-facing CRUD for the per-connection WhatsApp contact exclusion list.

Every query is scoped by tenant AND connection; another tenant's connection or entry is
`not_found` (404). Adding is gated like creating a connection (plan grant + kill switch);
listing and removing are not: a tenant can always see and delete its own list. The full number
is never stored or logged: only HMAC keys, an encrypted display mask and an optional label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.channels import identity
from app.db.models.whatsapp import WhatsAppChat, WhatsAppConnection, WhatsAppContactExclusion
from app.services.whatsapp_exclusion import (
    LABEL_MAX,
    MAX_BULK_ENTRIES,
    MAX_EXCLUSIONS_PER_CONNECTION,
    normalize_exclusion_phone,
    phone_jid,
    purge_sessionless_chats,
)
from app.services.whatsapp_service import WhatsAppError, WhatsAppService

INVALID_PHONE = (
    "Enter the number with its country code, e.g. +234 801 234 5678. "
    "Numbers starting with 0 and no country code cannot be matched."
)


def exclusion_to_dict(entry: WhatsAppContactExclusion) -> dict:
    return {
        "id": str(entry.id),
        "phone_masked": entry.phone_masked,
        "label": entry.label,
        "created_at": entry.created_at,
    }


def _clean_label(label: str | None) -> str | None:
    cleaned = (label or "").strip()[:LABEL_MAX]
    return cleaned or None


@dataclass
class BulkResult:
    added: int = 0
    already_excluded: int = 0
    rejected: list[dict] = field(default_factory=list)


class WhatsAppExclusionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self._connections = WhatsAppService(db)

    def _connection(self, tenant_id: UUID, connection_id: UUID) -> WhatsAppConnection:
        return self._connections.get_for_tenant(tenant_id, connection_id)

    def _count(self, connection_id: UUID) -> int:
        return int(
            self.db.execute(
                select(func.count())
                .select_from(WhatsAppContactExclusion)
                .where(WhatsAppContactExclusion.connection_id == connection_id)
            ).scalar_one()
        )

    def _prepare(self, conn: WhatsAppConnection, phone_number: str) -> tuple[str, str, str]:
        """(digits, phone_hmac, mask) or WhatsAppError. The digits live only in this call."""
        digits = normalize_exclusion_phone(phone_number)
        if digits is None:
            raise WhatsAppError(422, "invalid_phone", INVALID_PHONE)
        try:
            key = identity.phone_hmac(digits)
        except identity.PepperMissing:
            logger.error("WHATSAPP_CONTACT_PEPPER is not configured")
            raise WhatsAppError(
                503, "whatsapp_misconfigured", "WhatsApp is temporarily unavailable."
            ) from None
        if key == conn.phone_hmac:
            raise WhatsAppError(
                422, "own_number", "That is the number connected to Luran; it cannot be excluded."
            )
        return digits, key, identity.mask_phone(digits)

    def _insert(
        self, conn: WhatsAppConnection, digits: str, key: str, mask: str, label: str | None
    ) -> WhatsAppContactExclusion | None:
        """The new entry, or None when the number is already listed. Flushes, does not commit."""
        entry = WhatsAppContactExclusion(
            tenant_id=conn.tenant_id,
            connection_id=conn.id,
            phone_hmac=key,
            phone_masked=mask,
            label=label,
        )
        try:
            with self.db.begin_nested():
                self.db.add(entry)
        except IntegrityError:
            return None
        # Chats this contact already has: by the number the sidecar resolved on an earlier message,
        # or by the phone-JID ref. A LID-keyed chat teaches the entry its LID key right away, so
        # the contact is recognised before any new event; pause-only rows (no conversation) go.
        jid_key = identity.contact_hmac(phone_jid(digits))
        chats = list(
            self.db.execute(
                select(WhatsAppChat).where(
                    WhatsAppChat.connection_id == conn.id,
                    or_(WhatsAppChat.phone_hmac == key, WhatsAppChat.contact_hmac == jid_key),
                )
            ).scalars()
        )
        lid_chat = next((c for c in chats if c.contact_ref.endswith("@lid")), None)
        if lid_chat is not None:
            entry.lid_hmac = lid_chat.contact_hmac
        purge_sessionless_chats(self.db, conn.id, [c.contact_hmac for c in chats])
        return entry

    # ------------------------------------------------------------------ add
    def add(
        self, tenant_id: UUID, connection_id: UUID, phone_number: str, label: str | None
    ) -> WhatsAppContactExclusion:
        self._connections._require_enabled(tenant_id)
        conn = self._connection(tenant_id, connection_id)
        digits, key, mask = self._prepare(conn, phone_number)
        if self._count(conn.id) >= MAX_EXCLUSIONS_PER_CONNECTION:
            raise self._limit_error()
        entry = self._insert(conn, digits, key, mask, _clean_label(label))
        if entry is None:
            raise WhatsAppError(
                409, "already_excluded", "This number is already on the exclusion list."
            )
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def add_bulk(
        self, tenant_id: UUID, connection_id: UUID, entries: list[tuple[str, str | None]]
    ) -> BulkResult:
        """Partial success: valid new numbers are added, duplicates counted, bad rows named by
        index. The cap applies to the whole request (a request that would pass it adds nothing)."""
        self._connections._require_enabled(tenant_id)
        conn = self._connection(tenant_id, connection_id)
        if not 1 <= len(entries) <= MAX_BULK_ENTRIES:
            raise WhatsAppError(
                422, "invalid_bulk", f"Send between 1 and {MAX_BULK_ENTRIES} numbers per request."
            )
        result = BulkResult()
        prepared: dict[str, tuple[str, str, str | None]] = {}
        for index, (phone_number, label) in enumerate(entries):
            try:
                digits, key, mask = self._prepare(conn, phone_number)
            except WhatsAppError as exc:
                if exc.status_code == 503:
                    raise
                result.rejected.append({"index": index, "code": exc.code})
                continue
            if key in prepared:
                result.already_excluded += 1  # the same number twice in one request
                continue
            prepared[key] = (digits, mask, _clean_label(label))
        if self._count(conn.id) + len(prepared) > MAX_EXCLUSIONS_PER_CONNECTION:
            raise self._limit_error()
        for key, (digits, mask, label) in prepared.items():
            if self._insert(conn, digits, key, mask, label) is None:
                result.already_excluded += 1
            else:
                result.added += 1
        self.db.commit()
        return result

    @staticmethod
    def _limit_error() -> WhatsAppError:
        return WhatsAppError(
            409,
            "exclusion_limit",
            f"A number can have at most {MAX_EXCLUSIONS_PER_CONNECTION} excluded contacts.",
        )

    # ------------------------------------------------------------------ list / remove
    def list(
        self, tenant_id: UUID, connection_id: UUID, limit: int, offset: int
    ) -> tuple[list[WhatsAppContactExclusion], int]:
        conn = self._connection(tenant_id, connection_id)
        rows = list(
            self.db.execute(
                select(WhatsAppContactExclusion)
                .where(WhatsAppContactExclusion.connection_id == conn.id)
                .order_by(WhatsAppContactExclusion.created_at, WhatsAppContactExclusion.id)
                .limit(limit)
                .offset(offset)
            ).scalars()
        )
        return rows, self._count(conn.id)

    def remove(self, tenant_id: UUID, connection_id: UUID, exclusion_id: UUID) -> None:
        conn = self._connection(tenant_id, connection_id)
        entry = self.db.execute(
            select(WhatsAppContactExclusion).where(
                WhatsAppContactExclusion.id == exclusion_id,
                WhatsAppContactExclusion.connection_id == conn.id,
                WhatsAppContactExclusion.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if entry is None:
            raise WhatsAppError(404, "not_found", "Excluded contact not found")
        self.db.delete(entry)
        self.db.commit()
