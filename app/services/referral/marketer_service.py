"""Platform-admin operations on marketers: onboard, edit payout details, suspend, reactivate."""

import re
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.encryption import hash_for_lookup
from app.db.models.referral import Marketer, MarketerStatus, ReferralReward, RewardStatus
from app.db.models.user import User
from app.services.email_service import email_service
from app.services.referral.codes import validate_marketer_code
from app.services.token_service import token_service

SUSPENDED_REASON = "marketer_suspended"
_ACCOUNT_NUMBER = re.compile(r"\d{10}")
_BANK_CODE_MAX = 10


class MarketerError(Exception):
    """Base class: ``code`` is the stable machine-readable error string."""

    code = "marketer_error"


class MarketerNotFoundError(MarketerError):
    code = "marketer_not_found"


class MarketerCodeTakenError(MarketerError):
    code = "code_taken"


class MarketerEmailInUseError(MarketerError):
    code = "email_in_use"


def normalise_email(email: str) -> str:
    """The single normalisation applied to every marketer email, on write and on lookup."""
    return email.strip().lower()


def _user_email_hash_candidates(email: str) -> set[str]:
    """Hashes a tenant ``User`` with this address could be stored under.

    ``User.email_hash`` hashes the address exactly as it was typed (registration paths do not
    lowercase it), so a lowercase lookup alone misses ``Owner@Acme.com``. Check the normalised
    form plus the casings people actually type; a stored casing outside this set is not caught.
    """
    local, _, domain = email.partition("@")
    variants = {
        email,
        email.upper(),
        f"{local.capitalize()}@{domain}",
        f"{local.title()}@{domain}",
        f"{local}@{domain.upper()}",
    }
    return {hash_for_lookup(v) for v in variants}


class MarketerService:
    def __init__(self, db: Session):
        self.db = db

    # --- helpers ------------------------------------------------------------------------------

    def _get_for_update(self, marketer_id: UUID) -> Marketer:
        marketer = self.db.execute(
            select(Marketer).where(Marketer.id == marketer_id).with_for_update()
        ).scalar_one_or_none()
        if marketer is None:
            raise MarketerNotFoundError("Marketer not found")
        return marketer

    def _code_taken(self, code: str) -> bool:
        return (
            self.db.execute(
                select(Marketer.id).where(func.lower(Marketer.code) == code.lower()).limit(1)
            ).first()
            is not None
        )

    def _email_in_use(self, email: str) -> bool:
        if (
            self.db.execute(
                select(Marketer.id).where(Marketer.email_hash == hash_for_lookup(email)).limit(1)
            ).first()
            is not None
        ):
            return True
        return (
            self.db.execute(
                select(User.id)
                .where(User.email_hash.in_(_user_email_hash_candidates(email)))
                .limit(1)
            ).first()
            is not None
        )

    @staticmethod
    def _validate_bank(
        bank_code: str, account_number: str, account_name: str
    ) -> tuple[str, str, str]:
        bank_code = (bank_code or "").strip()
        account_name = (account_name or "").strip()
        if not bank_code or len(bank_code) > _BANK_CODE_MAX:
            raise ValueError(f"Bank code must be 1-{_BANK_CODE_MAX} characters")
        if not _ACCOUNT_NUMBER.fullmatch(account_number or ""):
            raise ValueError("Account number must be exactly 10 digits")
        if not account_name:
            raise ValueError("Account name is required")
        return bank_code, account_number, account_name

    # --- reads --------------------------------------------------------------------------------

    def get(self, marketer_id: UUID) -> Marketer:
        marketer = self.db.get(Marketer, marketer_id)
        if marketer is None:
            raise MarketerNotFoundError("Marketer not found")
        return marketer

    def list(
        self, status: MarketerStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[Marketer], int]:
        query = select(Marketer)
        count_query = select(func.count()).select_from(Marketer)
        if status is not None:
            query = query.where(Marketer.status == status)
            count_query = count_query.where(Marketer.status == status)
        total = self.db.execute(count_query).scalar_one()
        rows = (
            self.db.execute(
                query.order_by(Marketer.created_at.desc(), Marketer.id).limit(limit).offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    # --- writes -------------------------------------------------------------------------------

    def create(
        self,
        admin_id: UUID | None,
        name: str,
        email: str,
        code: str,
        bank_code: str,
        account_number: str,
        account_name: str,
    ) -> Marketer:
        """Onboard a marketer and email the one-time set-password link.

        Raises ``ValueError`` (invalid code / bank details), ``MarketerCodeTakenError`` or
        ``MarketerEmailInUseError`` (also when the email belongs to a tenant user).
        """
        code = validate_marketer_code(code)
        bank_code, account_number, account_name = self._validate_bank(
            bank_code, account_number, account_name
        )
        email = normalise_email(email)
        name = name.strip()
        if not name:
            raise ValueError("Name is required")

        if self._code_taken(code):
            raise MarketerCodeTakenError("Referral code already in use")
        if self._email_in_use(email):
            raise MarketerEmailInUseError("Email already in use")

        marketer = Marketer(
            name=name,
            email=email,
            code=code,
            bank_code=bank_code,
            account_number=account_number,
            account_name=account_name,
            created_by_admin_id=admin_id,
        )
        savepoint = self.db.begin_nested()  # a lost race must not poison the session
        try:
            self.db.add(marketer)
            self.db.flush()
            savepoint.commit()
        except IntegrityError as exc:
            savepoint.rollback()
            # Lost a race against a concurrent create: unique(lower(code)) or unique(email_hash).
            if self._code_taken(code):
                raise MarketerCodeTakenError("Referral code already in use") from exc
            raise MarketerEmailInUseError("Email already in use") from exc
        self.db.commit()
        self.db.refresh(marketer)

        self._send_invite(marketer)
        return marketer

    def _send_invite(self, marketer: Marketer) -> None:
        """Email the invite. The marketer row is already committed, so never raise from here."""
        try:
            token = token_service.create_marketer_invite_token(marketer.id, marketer.email)
            sent = email_service.send_marketer_invite_email(
                marketer_email=marketer.email,
                marketer_name=marketer.name,
                code=marketer.code,
                invite_token=token,
            )
            if not sent:
                logger.error(f"Marketer invite email was not sent for marketer {marketer.id}")
        except Exception as exc:
            logger.error(f"Failed to send marketer invite for marketer {marketer.id}: {exc}")

    def update_bank_details(
        self, marketer_id: UUID, bank_code: str, account_number: str, account_name: str
    ) -> Marketer:
        bank_code, account_number, account_name = self._validate_bank(
            bank_code, account_number, account_name
        )
        marketer = self._get_for_update(marketer_id)
        marketer.bank_code = bank_code
        marketer.account_number = account_number
        marketer.account_name = account_name
        # A stored transfer recipient points at the old account: force re-creation on next payout.
        marketer.paystack_recipient_code = None
        self.db.commit()
        self.db.refresh(marketer)
        return marketer

    def suspend(self, marketer_id: UUID) -> Marketer:
        """Suspend and hold the marketer's unpaid rewards. Safe to call twice."""
        marketer = self._get_for_update(marketer_id)
        marketer.status = MarketerStatus.SUSPENDED

        rewards = (
            self.db.execute(
                select(ReferralReward)
                .where(
                    ReferralReward.marketer_id == marketer_id,
                    ReferralReward.status.in_((RewardStatus.PENDING, RewardStatus.PAYABLE)),
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
        for reward in rewards:
            reasons = list(reward.review_reasons or [])
            if SUSPENDED_REASON not in reasons:
                reasons.append(SUSPENDED_REASON)
            reward.review_reasons = reasons  # new list: JSON columns do not track in-place edits
            reward.status = RewardStatus.HELD
        self.db.commit()
        self.db.refresh(marketer)
        return marketer

    def reactivate(self, marketer_id: UUID) -> Marketer:
        """Set the marketer active again. Held rewards stay held: an admin releases them."""
        marketer = self._get_for_update(marketer_id)
        marketer.status = MarketerStatus.ACTIVE
        self.db.commit()
        self.db.refresh(marketer)
        return marketer
