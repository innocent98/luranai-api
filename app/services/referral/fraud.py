"""Fraud flags for referral rewards: signals for an admin to review, never a reason to block.

``evaluate_flags`` is called inside the first-payment webhook transaction (flag-and-hold: a flagged
reward is held for review, the signup and the payment always go through). It is therefore
read-only, issues a handful of small queries, and is tolerant of odd stored data: a malformed
domain or payload is skipped, never raised. It does not swallow database errors, which would leave
a Postgres transaction aborted behind the caller's back; those propagate.

A person who runs several different businesses is legitimate, so sharing a *marketer* is never a
flag by itself; the signals are shared infrastructure (domain, card, origin), self-referral, and
unnatural signup volume.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.payment import PaymentTransaction
from app.db.models.referral import ReferralAttribution, SignupOrigin
from app.db.models.tenant import Tenant
from app.services.referral.domains import tenant_domains, tenant_domains_by_tenant

SHARED_DOMAIN = "shared_domain"
SHARED_CARD = "shared_card"
SHARED_ORIGIN = "shared_origin"
SELF_REFERRAL = "self_referral"
MARKETER_BURST = "marketer_burst"

# Fixed order of the returned reasons.
FLAG_ORDER = (SHARED_DOMAIN, SHARED_CARD, SHARED_ORIGIN, SELF_REFERRAL, MARKETER_BURST)

BURST_WINDOW = timedelta(hours=24)
BURST_THRESHOLD = 10  # strictly more than this many attributions in the window flags


@dataclass(frozen=True)
class SharedDomainMatch:
    """Another tenant that shares a registrable domain with the one under review."""

    other_tenant_id: UUID
    other_tenant_name: str  # the other tenant's display name
    domain: str  # the shared registrable domain


def shared_domain_tenants(db: Session, tenant_id: UUID) -> list[SharedDomainMatch]:
    """Every other tenant (referred or not, any marketer) sharing a domain with ``tenant_id``.

    One entry per (other tenant, domain), sorted by tenant name, then domain, then tenant id.
    """
    mine = tenant_domains(db, tenant_id)
    if not mine:
        return []
    pairs = {
        (other_id, domain)
        for other_id, domains in tenant_domains_by_tenant(db).items()
        if other_id != tenant_id
        for domain in domains & mine
    }
    if not pairs:
        return []
    names = dict(
        db.execute(
            select(Tenant.id, Tenant.display_name).where(Tenant.id.in_({p[0] for p in pairs}))
        ).all()
    )
    matches = [SharedDomainMatch(oid, names.get(oid) or "", domain) for oid, domain in pairs]
    return sorted(matches, key=lambda m: (m.other_tenant_name, m.domain, str(m.other_tenant_id)))


def _card_signature(paystack_data) -> str | None:
    """``data.authorization.signature`` of a stored charge.success payload, if usable.

    Paystack fingerprints a card with ``authorization.signature`` (null/absent for non-card
    channels). Anything that is not a non-empty string is treated as absent.
    """
    if not isinstance(paystack_data, dict):
        return None
    authorization = paystack_data.get("authorization")
    if not isinstance(authorization, dict):
        return None
    signature = authorization.get("signature")
    if isinstance(signature, str) and signature.strip():
        return signature.strip()
    return None


def _successful_signatures(rows) -> set[str]:
    signatures: set[str] = set()
    for (payload,) in rows:
        try:
            signature = _card_signature(payload)
        except Exception:  # noqa: BLE001 - one bad row must not hide the others
            logger.warning("fraud flags: unreadable payment payload; skipping it")
            continue
        if signature:
            signatures.add(signature)
    return signatures


def _shares_card(db: Session, tenant_id: UUID, marketer_id: UUID) -> bool:
    """Same card signature on a successful payment of another tenant of the same marketer."""
    mine = _successful_signatures(
        db.execute(
            select(PaymentTransaction.paystack_data).where(
                PaymentTransaction.tenant_id == tenant_id,
                PaymentTransaction.status == "success",
            )
        )
    )
    if not mine:
        return False
    theirs = _successful_signatures(
        db.execute(
            select(PaymentTransaction.paystack_data)
            .join(
                ReferralAttribution, ReferralAttribution.tenant_id == PaymentTransaction.tenant_id
            )
            .where(
                ReferralAttribution.marketer_id == marketer_id,
                PaymentTransaction.tenant_id != tenant_id,
                PaymentTransaction.status == "success",
            )
        )
    )
    return bool(mine & theirs)


def _shares_origin(db: Session, tenant_id: UUID, marketer_id: UUID) -> bool:
    """Another tenant of the same marketer signed up from the same (hashed) IP + user agent."""
    own_hash = db.scalar(
        select(SignupOrigin.origin_hash).where(SignupOrigin.tenant_id == tenant_id)
    )
    if not own_hash:
        return False
    return (
        db.scalar(
            select(SignupOrigin.id)
            .join(ReferralAttribution, ReferralAttribution.tenant_id == SignupOrigin.tenant_id)
            .where(
                ReferralAttribution.marketer_id == marketer_id,
                SignupOrigin.tenant_id != tenant_id,
                SignupOrigin.origin_hash == own_hash,
            )
            .limit(1)
        )
        is not None
    )


def _is_burst(db: Session, attribution: ReferralAttribution) -> bool:
    """True when the marketer has more than 10 attributions in the 24 hours up to this one.

    Window: ``attributed_at`` of this tenant's attribution, looking back 24 hours; both ends are
    inclusive (``attribution - 24h <= attributed_at <= attribution``). The tenant's own
    attribution is counted, so the 11th referred signup inside the window is flagged and the 10th
    is not ("more than 10" is strictly greater). Signups after this attribution are not counted.
    """
    end = attribution.attributed_at
    count = db.scalar(
        select(func.count(ReferralAttribution.id)).where(
            ReferralAttribution.marketer_id == attribution.marketer_id,
            ReferralAttribution.attributed_at >= end - BURST_WINDOW,
            ReferralAttribution.attributed_at <= end,
        )
    )
    return (count or 0) > BURST_THRESHOLD


def evaluate_flags(db: Session, tenant_id: UUID) -> list[str]:
    """Reasons to hold the tenant's reward for review, in the fixed order of ``FLAG_ORDER``.

    ``shared_domain`` applies to any tenant. The rest compare against other tenants of the *same*
    marketer (or the marketer's behaviour), so an unattributed tenant gets none of them.
    Read-only; returns ``[]`` for an unknown tenant.
    """
    flags: list[str] = []
    if shared_domain_tenants(db, tenant_id):
        flags.append(SHARED_DOMAIN)

    attribution = db.scalar(
        select(ReferralAttribution).where(ReferralAttribution.tenant_id == tenant_id)
    )
    if attribution is None:
        return flags

    if _shares_card(db, tenant_id, attribution.marketer_id):
        flags.append(SHARED_CARD)
    if _shares_origin(db, tenant_id, attribution.marketer_id):
        flags.append(SHARED_ORIGIN)
    if attribution.self_referral:
        flags.append(SELF_REFERRAL)
    if _is_burst(db, attribution):
        flags.append(MARKETER_BURST)
    return flags
