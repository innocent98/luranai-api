"""Payout providers: how a marketer's batch item actually gets paid.

``pay`` is called by ``payouts`` only AFTER the item has been claimed (``processing``, reference
and amount stored and committed), with no database row locks held. A provider must therefore be
safe to call again with the same ``reference``: Paystack deduplicates on it.

``verify`` answers "does a transfer with this reference exist, and in what state?". The payout
retry path calls it BEFORE ever sending again, so ``not_found`` must only be returned when the
provider is sure nothing was created; anything it cannot determine raises ``PayoutProviderError``.

Nothing here logs bank details, recipient codes or the Authorization header.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.core.config import settings
from app.db.models.referral import Marketer
from app.services.paystack_service import PaystackService, PaystackTransferError

TransferStatus = Literal["success", "pending", "failed", "otp", "manual"]
VerifyStatus = Literal["success", "pending", "failed", "reversed", "not_found"]

# Paystack verify statuses that mean the money is still moving or waiting on someone.
_PENDING_STATUSES = frozenset({"pending", "otp", "received", "queued", "processing"})


@dataclass(frozen=True)
class TransferResult:
    """Outcome of ``pay``.

    ``success`` -> paid; ``pending`` -> accepted but not final (webhook / verify finalises it);
    ``failed`` -> definitively not sent; ``otp`` -> Paystack wants an OTP to finalise (the item
    fails with ``otp_required`` and the admin falls back to a manual payment); ``manual`` -> no
    money moved, a human has to pay and record it.
    """

    status: TransferStatus
    reference: str
    provider_status: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class TransferVerification:
    status: VerifyStatus
    reference: str
    provider_status: str | None = None
    amount_kobo: int | None = None
    message: str | None = None


class PayoutProviderError(Exception):
    """The provider could not tell us the state of a transfer. The caller must not assume."""


class PayoutProvider(Protocol):
    def pay(
        self, marketer: Marketer, amount_kobo: int, reference: str, *, reason: str | None = None
    ) -> TransferResult: ...

    def verify(self, reference: str) -> TransferVerification: ...


class ManualPayoutProvider:
    """No money moves: the item waits for an admin to pay outside and record the bank reference."""

    def pay(
        self, marketer: Marketer, amount_kobo: int, reference: str, *, reason: str | None = None
    ) -> TransferResult:
        return TransferResult(status="manual", reference=reference, provider_status="manual")

    def verify(self, reference: str) -> TransferVerification:
        # This provider never creates a transfer, so it cannot vouch that a Paystack-era
        # reference does not exist. Refusing is the safe answer.
        raise PayoutProviderError("The manual provider cannot verify a transfer")


class PaystackTransferProvider:
    """Paystack Transfers. One recipient per marketer, created once and cached on the marketer."""

    def __init__(self, db: Session | None = None, service: PaystackService | None = None):
        self._db = db
        self._service = service

    def _paystack(self, db: Session | None) -> PaystackService:
        if self._service is not None:
            return self._service
        session = db or self._db
        if session is None:
            raise PayoutProviderError("PaystackTransferProvider needs a database session")
        return PaystackService(session)

    def pay(
        self, marketer: Marketer, amount_kobo: int, reference: str, *, reason: str | None = None
    ) -> TransferResult:
        db = object_session(marketer) or self._db
        if db is None:
            raise PayoutProviderError("marketer is not attached to a session")
        service = self._paystack(db)
        # Copy what we need, then end the read transaction: no connection is held, and no
        # lock of any kind, across the HTTP calls below.
        marketer_id = marketer.id
        recipient_code = marketer.paystack_recipient_code
        bank = (marketer.account_name, marketer.account_number, marketer.bank_code)
        db.commit()

        if not recipient_code:
            if not all(bank):
                return TransferResult(
                    status="failed", reference=reference, message="bank_details_missing"
                )
            try:
                recipient_code = service.create_transfer_recipient(
                    name=bank[0], account_number=bank[1], bank_code=bank[2]
                )
            except PaystackTransferError as exc:
                logger.warning(f"transfer recipient not created for marketer {marketer_id}")
                return TransferResult(
                    status="failed", reference=reference, message=f"recipient_error: {exc}"[:200]
                )
            if not self._store_recipient(db, marketer_id, recipient_code, bank):
                # The bank details changed while the recipient was being created: the code points
                # at the OLD account. Nothing was sent; the retry uses the new details.
                return TransferResult(
                    status="failed", reference=reference, message="bank_details_changed"
                )

        try:
            outcome = service.initiate_transfer(
                amount_kobo=amount_kobo,
                recipient_code=recipient_code,
                reference=reference,
                reason=reason or "Luran referral payout",
            )
        except PaystackTransferError as exc:
            if exc.definitive:
                return TransferResult(
                    status="failed",
                    reference=reference,
                    provider_status="rejected",
                    message=str(exc)[:200],
                )
            # Ambiguous (timeout / 5xx): the transfer may exist. Leave the item processing; the
            # verify-first retry resolves it. NEVER report failure here.
            logger.warning(f"transfer {reference} outcome unknown ({type(exc).__name__})")
            return TransferResult(
                status="pending",
                reference=reference,
                provider_status="unconfirmed",
                message="outcome unknown; resolve with verify",
            )

        status = outcome["status"]
        if status == "success":
            mapped: TransferStatus = "success"
        elif status == "otp":
            mapped = "otp"
        elif status in ("failed", "reversed"):
            mapped = "failed"
        else:
            mapped = "pending"  # pending / queued / received / anything not yet final
        return TransferResult(
            status=mapped,
            reference=reference,
            provider_status=status,
            message=outcome.get("message"),
        )

    @staticmethod
    def _store_recipient(
        db: Session, marketer_id, recipient_code: str, bank: tuple[str, str, str]
    ) -> bool:
        """Cache the code unless the bank details moved on; True when the code is usable."""
        marketer = db.scalar(
            select(Marketer)
            .where(Marketer.id == marketer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        try:
            if marketer is None:
                return False
            current = (marketer.account_name, marketer.account_number, marketer.bank_code)
            if current != bank:
                return False
            if not marketer.paystack_recipient_code:
                marketer.paystack_recipient_code = recipient_code
            return True
        finally:
            db.commit()

    def verify(self, reference: str) -> TransferVerification:
        try:
            raw = self._paystack(None).verify_transfer(reference)
        except PaystackTransferError as exc:
            raise PayoutProviderError(f"could not verify transfer: {exc}"[:200]) from exc
        provider_status = str(raw.get("status", "")).lower()
        amount = raw.get("amount_kobo")
        if provider_status == "not_found":
            mapped: VerifyStatus = "not_found"
        elif provider_status in ("success", "failed", "reversed"):
            mapped = provider_status  # type: ignore[assignment]
        elif provider_status in _PENDING_STATUSES:
            mapped = "pending"
        else:
            raise PayoutProviderError(f"unknown transfer status {provider_status!r}")
        return TransferVerification(
            status=mapped,
            reference=reference,
            provider_status=provider_status,
            amount_kobo=amount if isinstance(amount, int) else None,
            message=raw.get("message"),
        )


def get_payout_provider(db: Session | None = None) -> PayoutProvider:
    """The provider chosen by ``REFERRAL_PAYOUT_PROVIDER`` (``manual`` unless set otherwise).

    The Paystack provider needs a session for ``verify`` (it builds a ``PaystackService``);
    ``pay`` takes the session from the marketer it is given.
    """
    if settings.REFERRAL_PAYOUT_PROVIDER == "paystack":
        return PaystackTransferProvider(db)
    return ManualPayoutProvider()
