"""LeadService — score orchestration, list/detail, status lifecycle, summary."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import cast, func, select
from sqlalchemy import Integer as SAInteger
from sqlalchemy.orm import Session

from app.db.models.lead import Lead, LeadStatus
from app.sales.intent import normalize_intent
from app.sales.scoring import TIER_THRESHOLDS, score_lead

# Same pattern as CONTACT_EMAIL_PATTERN in app/api/v1/endpoints/chat.py.
# Kept here (not imported) to avoid reversing the service→endpoint dependency.
_EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

_CAPTURED_FIELDS = ("name", "email", "company", "team_size", "industry")


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

# The dashboard's sort control emits the first four. "score" and "captured" are
# the original v1 names, kept as aliases rather than removed: /api/v1 has other
# clients, so dropping them would be a breaking change instead of an additive one.
SortKey = Literal[
    "score_desc",
    "score_asc",
    "created_desc",
    "created_asc",
    "score",
    "captured",
]

_SORT_ALIASES: dict[str, str] = {"score": "score_desc", "captured": "created_desc"}


class LeadService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # apply_score
    # ------------------------------------------------------------------

    def apply_score(
        self, lead: Lead, signals: dict | None = None, *, hold_peak: bool = False
    ) -> None:
        """Score the lead in-place, writing lead_score and score_breakdown.

        `hold_peak` is for automatic re-scoring from a chat turn. The engine only sees the signals
        of the CURRENT turn, so a plain follow-up would otherwise lower a lead that already
        reached sales-ready (and already alerted the rep). With it set the stored score and its
        breakdown are kept unless the new score is at least as high. Explicit human rescoring
        (`recompute`) leaves it off, so a rep's rescore still replaces the figure.

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
        if hold_peak and (lead.lead_score or 0) > result["score"]:
            return
        lead.lead_score = result["score"]
        lead.score_breakdown = result["breakdown"]

    # ------------------------------------------------------------------
    # get_or_create_by_conversation
    # ------------------------------------------------------------------

    def get_or_create_by_conversation(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        *,
        intent: str,
        source: str = "chat_widget",
        utm: dict | None = None,
    ) -> Lead:
        """Return the existing lead for this conversation, or create a new one.

        Idempotent per (tenant_id, conversation_id) pair.
        """
        existing = self.db.execute(
            select(Lead).where(
                Lead.tenant_id == tenant_id,
                Lead.conversation_id == conversation_id,
            )
        ).scalar_one_or_none()

        if existing is not None:
            return existing

        lead = Lead(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            intent=normalize_intent(intent),
            source=source,
            utm=utm or {},
            status=LeadStatus.NEW,
        )
        self.apply_score(lead)
        self.db.add(lead)
        self.db.flush()
        return lead

    # ------------------------------------------------------------------
    # enrich_from_chat
    # ------------------------------------------------------------------

    def enrich_from_chat(
        self,
        lead: Lead,
        *,
        intent: str,
        captured: dict,
        signals: dict,
    ) -> Lead:
        """Update lead fields from a chat-widget turn and re-score.

        Rules:
        - Only set a field if the incoming value is non-empty.
        - For email: only store if it passes the standard email-format check.
        - Never overwrite an existing non-empty field with null/empty.
        """
        for field in _CAPTURED_FIELDS:
            incoming = captured.get(field)
            if not incoming:
                # None, empty string, or absent — skip
                continue

            if field == "email":
                stripped = incoming.strip().lower()
                if not _EMAIL_PATTERN.fullmatch(stripped):
                    continue
                # Only set if lead.email is currently empty
                if not getattr(lead, "email", None):
                    lead.email = stripped
            else:
                # For all non-email fields: only fill empty slots
                if not getattr(lead, field, None):
                    setattr(lead, field, incoming)

        lead.intent = normalize_intent(intent)
        self.apply_score(lead, signals=signals, hold_peak=True)
        self.db.commit()
        self.db.refresh(lead)
        return lead

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

        # NULL scores sink to the bottom either way — an unscored lead is not
        # "the lowest-scoring lead", so it should not head the ascending list.
        key = _SORT_ALIASES.get(sort, sort)
        if key == "created_desc":
            query = query.order_by(Lead.created_at.desc())
        elif key == "created_asc":
            query = query.order_by(Lead.created_at.asc())
        elif key == "score_asc":
            query = query.order_by(Lead.lead_score.asc().nullslast())
        else:  # score_desc
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

    def get_crm_log(self, tenant_id: UUID, lead_id: UUID):
        """Return this lead's most recent CRM push record, or None if never tried.

        Scoped by channel and entity_id so a Slack or email notification for the
        same lead is never mistaken for a CRM delivery.
        """
        from app.db.models.notification_log import NotifChannel, NotificationLog

        return (
            self.db.query(NotificationLog)
            .filter(
                NotificationLog.tenant_id == tenant_id,
                NotificationLog.channel == NotifChannel.CRM,
                NotificationLog.entity_id == lead_id,
            )
            .order_by(NotificationLog.created_at.desc())
            .first()
        )

    def get_meeting(self, tenant_id: UUID, lead: Lead) -> dict | None:
        """Return this lead's booked meeting details, or None if no meeting booked."""
        from app.db.models.meeting import Meeting

        meeting = None
        if getattr(lead, "meeting_id", None):
            meeting = (
                self.db.query(Meeting)
                .filter(Meeting.tenant_id == tenant_id, Meeting.id == lead.meeting_id)
                .one_or_none()
            )
        if meeting is None:
            meeting = (
                self.db.query(Meeting)
                .filter(Meeting.tenant_id == tenant_id, Meeting.lead_id == lead.id)
                .order_by(Meeting.created_at.desc())
                .first()
            )
        if meeting is None:
            return None

        return {
            "id": str(meeting.id),
            "scheduled_at": meeting.scheduled_at.isoformat() if meeting.scheduled_at else None,
            "duration_min": meeting.duration_min,
            "visitor_timezone": meeting.visitor_timezone,
            "use_case": meeting.use_case,
            "status": (
                meeting.status.value if hasattr(meeting.status, "value") else str(meeting.status)
            ),
            "created_at": meeting.created_at.isoformat() if meeting.created_at else None,
        }

    def get_quote(self, tenant_id: UUID, lead: Lead) -> dict | None:
        """Return this lead's quote details, or None if no quote generated."""
        from app.db.models.quote import Quote

        quote = None
        if getattr(lead, "quote_id", None):
            quote = (
                self.db.query(Quote)
                .filter(Quote.tenant_id == tenant_id, Quote.id == lead.quote_id)
                .one_or_none()
            )
        if quote is None:
            quote = (
                self.db.query(Quote)
                .filter(Quote.tenant_id == tenant_id, Quote.lead_id == lead.id)
                .order_by(Quote.created_at.desc())
                .first()
            )
        if quote is None:
            return None

        return {
            "id": str(quote.id),
            "currency": quote.currency,
            "line_items": quote.line_items or [],
            "total": quote.total,
            "status": quote.status.value if hasattr(quote.status, "value") else str(quote.status),
            "created_at": quote.created_at.isoformat() if quote.created_at else None,
        }

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
        new_this_week: int = base.filter(Lead.created_at >= one_week_ago).count()

        # Sales-ready count (score >= 86)
        sales_ready_count: int = base.filter(
            Lead.lead_score >= TIER_THRESHOLDS["sales_ready"]
        ).count()

        return {
            "new_this_week": new_this_week,
            "avg_score": avg_score,
            "sales_ready_count": sales_ready_count,
            "pipeline_value": 0,
            "status_counts": status_counts,
        }

    # ------------------------------------------------------------------
    # crossed_sales_ready
    # ------------------------------------------------------------------

    def crossed_sales_ready(self, prior_score: int | None, lead: Lead) -> bool:
        """True iff the lead's score first reached the sales_ready threshold this update."""
        threshold = TIER_THRESHOLDS["sales_ready"]
        return (prior_score or 0) < threshold <= (lead.lead_score or 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tier_score_range(tier: str) -> tuple[int, int]:
    """Map tier name to inclusive [lo, hi] score range."""
    if tier == "cold":
        return 0, TIER_THRESHOLDS["warm"] - 1  # 0–30
    if tier == "warm":
        return TIER_THRESHOLDS["warm"], TIER_THRESHOLDS["hot"] - 1  # 31–60
    if tier == "hot":
        return TIER_THRESHOLDS["hot"], TIER_THRESHOLDS["sales_ready"] - 1  # 61–85
    if tier == "sales_ready":
        return TIER_THRESHOLDS["sales_ready"], 100  # 86–100
    raise ValueError(f"Unknown tier: {tier!r}")
