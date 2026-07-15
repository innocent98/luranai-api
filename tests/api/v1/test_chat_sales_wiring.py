"""Chat handler → sales LLM wiring.

Pins the two kwargs the endpoint feeds RAGService.generate_sales_response:

  * company_website — the tenant's latest audit URL, so the agent shares the
    real address instead of emitting "[insert URL here]".
  * known_facts — facts already on the conversation's Lead, so the agent stops
    re-asking for a team size the prospect just gave.

Both shipped without endpoint coverage and the stub signature drifted out of
step, which surfaced only as an opaque meta assertion in unrelated specs (the
endpoint funnels exceptions into meta["error"] and still answers 200). These
tests fail loudly and name the actual contract instead.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.db.models.audit import AuditReport, AuditScope, AuditStatus

from .test_chat_lead_capture import (
    _automation_patch,
    _make_tenant,
    _only_lead,
    _retrieval_patch,
)


def _spy_sales_llm(calls: list, intent: str = "PRICING_INQUIRY", captured: dict | None = None):
    """Patch generate_sales_response, recording the kwargs the endpoint passes."""

    async def _fake(
        self,
        prompt,
        config,
        *,
        model_name=None,
        known_facts=None,
        company_website=None,
    ):
        calls.append({"known_facts": known_facts, "company_website": company_website})
        sales_meta = {"intent": intent, "captured": captured or {}, "signals": {}}
        return "Happy to help!", sales_meta, 50, False

    return patch(
        "app.services.rag_service.RAGService.generate_sales_response",
        new=_fake,
    )


def _add_audit(db: Session, tenant_id: UUID, url: str, *, age_hours: int, status=AuditStatus.COMPLETED):
    db.add(
        AuditReport(
            id=uuid4(),
            tenant_id=tenant_id,
            url=url,
            domain=url.split("//")[-1].split("/")[0],
            scope=AuditScope.DASHBOARD,
            status=status,
            created_at=datetime.now(UTC) - timedelta(hours=age_hours),
        )
    )
    db.commit()


def _send(client, tenant_id: UUID, message: str, user: str = "v1"):
    return client.post(
        "/api/v1/chat/message",
        json={"message": message, "tenant_id": str(tenant_id), "user_identifier": user},
    )


class TestCompanyWebsite:
    def test_passes_latest_audit_url(self, client, db: Session):
        """The agent is handed the tenant's most recent audit URL."""
        tenant_id = _make_tenant(db, sales_enabled=True)
        _add_audit(db, tenant_id, "https://old.example.com/", age_hours=48)
        _add_audit(db, tenant_id, "https://weysure.example.com/", age_hours=1)

        calls: list = []
        with _spy_sales_llm(calls), _retrieval_patch(), _automation_patch():
            r = _send(client, tenant_id, "how much for bulk?")

        assert r.status_code == 200, r.text
        assert calls, "generate_sales_response was never called"
        assert calls[0]["company_website"] == "https://weysure.example.com/"

    def test_none_when_tenant_has_no_audit(self, client, db: Session):
        """No audit → no website. The prompt must omit the block, never invent one."""
        tenant_id = _make_tenant(db, sales_enabled=True)

        calls: list = []
        with _spy_sales_llm(calls), _retrieval_patch(), _automation_patch():
            r = _send(client, tenant_id, "how much for bulk?")

        assert r.status_code == 200, r.text
        assert calls[0]["company_website"] is None

    def test_ignores_other_tenants_audits(self, client, db: Session):
        """Tenant isolation: one tenant's audit URL must never leak into another's chat."""
        tenant_id = _make_tenant(db, sales_enabled=True)
        other_id = _make_tenant(db, sales_enabled=True)
        _add_audit(db, other_id, "https://not-mine.example.com/", age_hours=1)

        calls: list = []
        with _spy_sales_llm(calls), _retrieval_patch(), _automation_patch():
            r = _send(client, tenant_id, "how much for bulk?")

        assert r.status_code == 200, r.text
        assert calls[0]["company_website"] is None


class TestKnownFacts:
    def test_none_on_first_turn(self, client, db: Session):
        """No lead exists yet, so there is nothing to pin."""
        tenant_id = _make_tenant(db, sales_enabled=True)

        calls: list = []
        with _spy_sales_llm(calls), _retrieval_patch(), _automation_patch():
            r = _send(client, tenant_id, "how much for bulk?")

        assert r.status_code == 200, r.text
        assert calls[0]["known_facts"] is None

    def test_second_turn_pins_facts_captured_on_the_first(self, client, db: Session):
        """The team size given on turn 1 is fed back on turn 2 — the re-ask bug."""
        tenant_id = _make_tenant(db, sales_enabled=True)

        calls: list = []
        captured = {"name": "Ada", "email": "ada@co.com", "team_size": "10-15"}
        with _spy_sales_llm(calls, captured=captured), _retrieval_patch(), _automation_patch():
            r1 = _send(client, tenant_id, "how much for bulk?")
            assert r1.status_code == 200, r1.text
            session_id = r1.json()["session_id"]

            r2 = client.post(
                "/api/v1/chat/message",
                json={
                    "message": "and for 20 seats?",
                    "tenant_id": str(tenant_id),
                    "user_identifier": "v1",
                    "session_id": session_id,
                },
            )

        assert r2.status_code == 200, r2.text
        assert _only_lead(db, tenant_id).team_size == "10-15"
        assert len(calls) == 2, f"expected two LLM turns, got {len(calls)}"
        facts = calls[1]["known_facts"]
        assert facts is not None, "turn 2 must pin the facts captured on turn 1"
        assert facts["team_size"] == "10-15"
        assert facts["name"] == "Ada"
        assert facts["email"] == "ada@co.com"
