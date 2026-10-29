"""Attribute a new tenant to the marketer whose referral code it signed up with.

Signup is revenue-critical, so nothing here may block or alter it: ``attribute_signup`` returns
``None`` instead of raising, for bad codes and for database trouble alike. Callers learn nothing
about which codes exist (there is no distinguishable outcome beyond "attributed or not", and the
signup response never reflects it).
"""

import hashlib
import hmac

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.referral import (
    AttributionSource,
    Marketer,
    MarketerStatus,
    ReferralAttribution,
    SignupOrigin,
)
from app.db.models.tenant import Tenant
from app.services.referral.codes import validate_marketer_code
from app.services.referral.marketer_service import normalise_email

# Shared mail providers: two people on gmail.com are not the same organisation.
COMMON_MAIL_DOMAINS = frozenset(
    {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}
)

_ORIGIN_KEY_LABEL = b"luran-referral-origin-v1"


def _origin_key() -> bytes:
    """Domain-separated key derived from SECRET_KEY (no extra env var to manage)."""
    return hmac.new(settings.SECRET_KEY.encode(), _ORIGIN_KEY_LABEL, hashlib.sha256).digest()


def origin_hash(ip: str | None, user_agent: str | None) -> str:
    """Keyed hash (HMAC-SHA256 hex) of the signup IP and user agent; the raw values are not kept.

    The two parts are length-prefixed so ("ab", "c") and ("a", "bc") cannot collide.
    """
    ip_bytes = (ip or "").encode()
    ua_bytes = (user_agent or "").encode()
    message = len(ip_bytes).to_bytes(4, "big") + ip_bytes + ua_bytes
    return hmac.new(_origin_key(), message, hashlib.sha256).hexdigest()


def _domain(email: str) -> str:
    return email.rpartition("@")[2]


def _is_self_referral(signup_email: str, marketer_email: str) -> bool:
    """True when the signup address is the marketer's, or shares a non-shared-provider domain."""
    signup, marketer = normalise_email(signup_email), normalise_email(marketer_email)
    if signup == marketer:
        return True
    domain = _domain(signup)
    return bool(domain) and domain == _domain(marketer) and domain not in COMMON_MAIL_DOMAINS


def _resolve_active_marketer(db: Session, referral_code: str) -> Marketer | None:
    """The active marketer owning this code (case-insensitive), or None for any unusable code."""
    if not isinstance(referral_code, str):
        return None
    try:
        code = validate_marketer_code(referral_code)
    except ValueError:
        return None
    return db.execute(
        select(Marketer).where(
            func.lower(Marketer.code) == code, Marketer.status == MarketerStatus.ACTIVE
        )
    ).scalar_one_or_none()


def attribute_signup(
    db: Session,
    tenant: Tenant,
    admin_email: str,
    referral_code: str | None,
    source: AttributionSource,
    origin_hash: str,
) -> ReferralAttribution | None:
    """Link ``tenant`` to the marketer owning ``referral_code``; None when nothing is attributed.

    Never raises on bad input or database problems. All writes happen in a savepoint, so a failure
    cannot poison the caller's session. The caller commits. A tenant is attributed at most once:
    a second call returns the existing attribution untouched (first link wins). The
    ``SignupOrigin`` row is written only together with a new attribution.
    """
    if not referral_code or not isinstance(referral_code, str):
        return None
    tenant_id = None
    try:
        tenant_id = tenant.id
        with db.begin_nested():
            existing = db.execute(
                select(ReferralAttribution).where(ReferralAttribution.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing

            marketer = _resolve_active_marketer(db, referral_code)
            if marketer is None:
                return None

            attribution = ReferralAttribution(
                tenant_id=tenant_id,
                marketer_id=marketer.id,
                source=source,
                self_referral=_is_self_referral(admin_email, marketer.email),
            )
            db.add(attribution)
            db.add(SignupOrigin(tenant_id=tenant_id, origin_hash=origin_hash))
            db.flush()
            return attribution
    except IntegrityError:
        # Lost a race with another attribution for the same tenant; the savepoint is rolled back.
        logger.info(f"Referral attribution already exists for tenant {tenant_id}")
        return None
    except Exception as exc:  # noqa: BLE001 - attribution must never break a signup
        logger.warning(f"Referral attribution skipped for tenant {tenant_id}: {type(exc).__name__}")
        return None
