"""Deep links from rep alerts (email, Slack) into the tenant dashboard."""

from uuid import UUID

from app.core.config import settings

# Route in luran-dashboard: app/(dashboard)/dashboard/grow/leads/[leadId]/page.tsx
_LEADS_PATH = "/dashboard/grow/leads"


def lead_dashboard_url(lead_id: UUID | None) -> str | None:
    """Absolute dashboard URL for one lead, from the same FRONTEND_URL the other emails use.

    Falls back to the leads list when the lead is unknown, and to None (no link at all, never
    '#') when the base URL is not configured.
    """
    base = (settings.FRONTEND_URL or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}{_LEADS_PATH}/{lead_id}" if lead_id else f"{base}{_LEADS_PATH}"
