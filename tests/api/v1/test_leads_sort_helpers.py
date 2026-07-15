"""Fixtures shared by the leads sort specs.

Kept out of test_leads_sort.py so pytest does not collect these as tests.
"""

import uuid
from datetime import UTC, datetime, timedelta


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_tenant_user(db_session):
    """Create a fresh Tenant + TENANT_ADMIN User; return (user, token)."""
    from app.core.security import create_access_token
    from app.db.models.tenant import Tenant
    from app.db.models.user import User, UserRole

    prefix = uuid.uuid4().hex[:8]
    tenant = Tenant(
        name=f"tenant_{prefix}",
        display_name=f"Tenant {prefix}",
        api_key_hash=uuid.uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db_session.add(tenant)
    db_session.flush()

    user = User(
        email=f"user_{prefix}@example.com",
        hashed_password="$2b$12$placeholder_hash_not_real",
        full_name=f"User {prefix}",
        role=UserRole.TENANT_ADMIN,
        tenant_id=tenant.id,
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)

    token = create_access_token(
        subject=str(user.id),
        additional_claims={"role": user.role.value, "tenant_id": str(user.tenant_id)},
    )
    return user, token


def seed_leads_with_scores(db_session, tenant_id, scores, *, stagger_created: bool = False):
    """Create one Lead per entry in *scores* with that exact lead_score.

    Scores are set directly rather than derived via apply_score so ordering
    assertions read off known values. When stagger_created, each lead is created
    an hour apart in list order so created_at ordering is deterministic — rows
    inserted in the same transaction otherwise share a timestamp.
    """
    from app.db.models.lead import Lead, LeadStatus

    now = datetime.now(UTC)
    for i, score in enumerate(scores):
        lead = Lead(
            tenant_id=tenant_id,
            name=f"Lead {i}",
            email=f"lead{i}_{uuid.uuid4().hex[:6]}@example.com",
            status=LeadStatus.NEW,
            lead_score=score,
        )
        if stagger_created:
            lead.created_at = now - timedelta(hours=len(scores) - i)
        db_session.add(lead)
    db_session.flush()
