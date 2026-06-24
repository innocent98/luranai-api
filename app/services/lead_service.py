"""LeadService — score orchestration, list/detail, status lifecycle, summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import cast, func
from sqlalchemy import Integer as SAInteger
from sqlalchemy.orm import Session

from app.db.models.lead import Lead, LeadStatus
from app.sales.scoring import TIER_THRESHOLDS, score_lead


class LeadNotFound(ValueError):
    """Raised when a lead does not exist for the given tenant."""


class IllegalStatusTransition(ValueError):
    """Raised when the requested status transition is not permitted."""


# Allowed transitions: current_status -> set of reachable next statuses
_ALLOWED_TRANSITIONS: dict[LeadStatus, set[LeadStatus]] = {
    LeadStatus.NEW: {LeadStatus.CONTACTED, LeadStatus.QUALIFIED, LeadStatus.CONVERTED},
    LeadStatus.CONTACTED: {LeadStatus.QUALIFIED, LeadStatus.CONVERTED},
    LeadStatus.QUALIFIED: {LeadStatus.CONVERTED},
    LeadStatus.CONVERTED: set(),
}

SortKey = Literal["score", "captured"]


class LeadService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # apply_score
    # ------------------------------------------------------------------

    def apply_score(self, lead: Lead, signals: dict | None = None) -> None:
        """Score the lead in-place, writing lead_score and score_breakdown.

        team_size is stored as String on the Lead model but the scoring engine
        compares it numerically (> 50).  We coerce here so real Lead rows work
        identically to SimpleNamespace fixtures in unit tests.
        """
        # Proxy object that presents team_size as an int so the engine works
        # even when the DB column holds a string value.
        class _LeadProxy:
            def __init__(self, lead: Lead) -> None:
                self._lead = lead

            def __getattr__(self, name: str):  # type: ignore[override]
                if name == "team_size":
                    raw = self._lead.team_size
                    if raw is None:
                        return None
                    try:
                        return int(raw)
                    except (ValueError, TypeError):
                        return 0
                return getattr(self._lead, name)

        result = score_lead(_LeadProxy(lead), signals)
        lead.lead_score = result["score"]
        lead.score_breakdown = result["breakdown"]

    # ------------------------------------------------------------------
    # list_leads
    # ------------------------------------------------------------------

    def list_leads(
        self,
        tenant_id: UUID,
        *,
        status: LeadStatus | None = None,
        intent: str | None = None,
        tier: str | None = None,
        sort: SortKey = "score",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        """Return (rows, total_count) scoped to tenant_id.

        Tier filtering is done via score ranges since tier is not stored:
          cold:        0 – 30
          warm:       31 – 60
          hot:        61 – 85
          sales_ready: 86 – 100
        """
        query = self.db.query(Lead).filter(Lead.tenant_id == tenant_id)

        if status is not None:
            query = query.filter(Lead.status == status)

        if intent is not None:
            query = query.filter(Lead.intent == intent)

        if tier is not None:
            lo, hi = _tier_score_range(tier)
            query = query.filter(Lead.lead_score >= lo, Lead.lead_score <= hi)

        total: int = query.count()

        if sort == "captured":
            query = query.order_by(Lead.created_at.desc())
        else:
            # Default: sort by lead_score descending; NULL scores sink to bottom
            query = query.order_by(Lead.lead_score.desc().nullslast())

        rows: list[Lead] = query.offset(offset).limit(limit).all()
        return rows, total

    # ------------------------------------------------------------------
    # get_lead
    # ------------------------------------------------------------------

    def get_lead(self, tenant_id: UUID, lead_id: UUID) -> Lead | None:
        return (
            self.db.query(Lead)
            .filter(Lead.tenant_id == tenant_id, Lead.id == lead_id)
            .one_or_none()
        )

    # ------------------------------------------------------------------
    # set_status
    # ------------------------------------------------------------------

    def set_status(self, tenant_id: UUID, lead_id: UUID, status: LeadStatus) -> Lead:
        """Transition lead to new status.  Raises ValueError on illegal transitions
        or if the lead does not belong to the tenant.
        """
        lead = self.get_lead(tenant_id, lead_id)
        if lead is None:
            raise LeadNotFound(f"Lead {lead_id} not found for tenant {tenant_id}")

        allowed = _ALLOWED_TRANSITIONS.get(lead.status, set())
        if status not in allowed:
            raise IllegalStatusTransition(
                f"Transition {lead.status.value!r} → {status.value!r} is not allowed. "
                f"Allowed: {[s.value for s in allowed]}"
            )

        lead.status = status
        self.db.commit()
        self.db.refresh(lead)
        return lead

    # ------------------------------------------------------------------
    # recompute
    # ------------------------------------------------------------------

    def recompute(self, tenant_id: UUID, lead_id: UUID) -> Lead:
        """Re-run scoring for an existing lead and persist the result.
        Raises ValueError if the lead is not found for this tenant.
        """
        lead = self.get_lead(tenant_id, lead_id)
        if lead is None:
            raise LeadNotFound(f"Lead {lead_id} not found for tenant {tenant_id}")

        self.apply_score(lead)
        self.db.commit()
        self.db.refresh(lead)
        return lead

    # ------------------------------------------------------------------
    # summary
    # ------------------------------------------------------------------

    def summary(self, tenant_id: UUID) -> dict:
        """Return summary statistics for a tenant's lead pipeline.

        pipeline_value is always 0 in this slice (no deal-value field yet).
        """
        base = self.db.query(Lead).filter(Lead.tenant_id == tenant_id)

        # Total and status breakdown
        status_counts: dict[str, int] = {s.value: 0 for s in LeadStatus}
        for status, count in (
            self.db.query(Lead.status, func.count(Lead.id))
            .filter(Lead.tenant_id == tenant_id)
            .group_by(Lead.status)
            .all()
        ):
            status_counts[status.value] = count

        # Average score (only over leads with a score)
        avg_score_raw = (
            self.db.query(func.avg(cast(Lead.lead_score, SAInteger)))
            .filter(Lead.tenant_id == tenant_id, Lead.lead_score.isnot(None))
            .scalar()
        )
        avg_score = round(float(avg_score_raw), 1) if avg_score_raw is not None else 0.0

        # New this week
        one_week_ago = datetime.now(UTC) - timedelta(days=7)
        new_this_week: int = (
            base.filter(Lead.created_at >= one_week_ago).count()
        )

        # Sales-ready count (score >= 86)
        sales_ready_count: int = (
            base.filter(Lead.lead_score >= TIER_THRESHOLDS["sales_ready"]).count()
        )

        return {
            "new_this_week": new_this_week,
            "avg_score": avg_score,
            "sales_ready_count": sales_ready_count,
            "pipeline_value": 0,
            "status_counts": status_counts,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tier_score_range(tier: str) -> tuple[int, int]:
    """Map tier name to inclusive [lo, hi] score range."""
    if tier == "cold":
        return 0, TIER_THRESHOLDS["warm"] - 1          # 0–30
    if tier == "warm":
        return TIER_THRESHOLDS["warm"], TIER_THRESHOLDS["hot"] - 1   # 31–60
    if tier == "hot":
        return TIER_THRESHOLDS["hot"], TIER_THRESHOLDS["sales_ready"] - 1  # 61–85
    if tier == "sales_ready":
        return TIER_THRESHOLDS["sales_ready"], 100      # 86–100
    raise ValueError(f"Unknown tier: {tier!r}")
