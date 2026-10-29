"""The platform-admin reward review queue: rewards with the evidence an admin needs to decide.

Read only. For rewards flagged ``shared_domain`` the matches are computed live (the same function
the fraud check uses) so the admin sees *which* other tenants share a domain. That computation is
limited to the page being returned, and a failure on one reward degrades to an empty list for that
reward alone (logged by exception class and ids), so one bad row never breaks the page.
"""

from uuid import UUID

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.referral import Marketer, ReferralReward, RewardStatus
from app.db.models.tenant import Tenant
from app.schemas.referral import (
    RewardQueueItem,
    RewardQueueResponse,
    SharedDomainMatchResponse,
)
from app.services.referral.fraud import SHARED_DOMAIN, shared_domain_tenants


def _shared_domain_matches(db: Session, reward_id: UUID, tenant_id: UUID | None) -> list:
    if tenant_id is None:
        return []
    try:
        # A SAVEPOINT, so a database error in this lookup cannot poison the rest of the page.
        with db.begin_nested():
            matches = shared_domain_tenants(db, tenant_id)
        return [
            SharedDomainMatchResponse(
                other_tenant_name=m.other_tenant_name,
                domain=m.domain,
                sources=list(m.sources),
                own_sources=list(m.own_sources),
            )
            for m in matches
        ]
    except Exception as exc:
        # Never the exception text: it can carry SQL parameters. Class and ids only.
        logger.error(
            f"shared-domain lookup failed for reward {reward_id} (tenant {tenant_id}): "
            f"{type(exc).__name__}"
        )
        return []


def list_rewards(
    db: Session, status: RewardStatus | None, limit: int, offset: int
) -> RewardQueueResponse:
    """Rewards in ``status`` (every status when ``None``), newest conversion first."""
    conditions = [] if status is None else [ReferralReward.status == status]
    total = db.scalar(select(func.count()).select_from(ReferralReward).where(*conditions)) or 0
    rows = db.execute(
        select(ReferralReward, Marketer, Tenant.display_name)
        .join(Marketer, Marketer.id == ReferralReward.marketer_id)
        .outerjoin(Tenant, Tenant.id == ReferralReward.tenant_id)
        .where(*conditions)
        .order_by(ReferralReward.converted_at.desc(), ReferralReward.id.desc())
        .limit(limit)
        .offset(offset)
        .execution_options(populate_existing=True)
    ).all()

    items = []
    for reward, marketer, company_name in rows:
        reasons = [str(r) for r in (reward.review_reasons or [])]
        matches = (
            _shared_domain_matches(db, reward.id, reward.tenant_id)
            if SHARED_DOMAIN in reasons
            else []
        )
        items.append(
            RewardQueueItem(
                id=reward.id,
                marketer_id=marketer.id,
                marketer_name=marketer.name,
                marketer_code=marketer.code,
                tenant_company_name=company_name,
                amount_kobo=reward.amount_kobo,
                status=reward.status,
                review_reasons=reasons,
                converted_at=reward.converted_at,
                payable_after=reward.payable_after,
                payout_batch_id=reward.payout_batch_id,
                shared_domain_matches=matches,
            )
        )
    return RewardQueueResponse(items=items, total=total, limit=limit, offset=offset)
