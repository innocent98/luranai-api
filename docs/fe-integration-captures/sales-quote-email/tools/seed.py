"""Seed two fake tenants for the live run: one with a rep address, one without (run from repo root)."""
import json
import sys
from uuid import uuid4

from app.db.models.sales_agent_config import TenantSalesAgentConfig
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities
from app.db.session import SessionLocal

TIER = {"name": "Growth", "min_users": 11, "max_users": 50, "base_price": 45000,
        "per_seat_overage_price": 3500}


def make(db, display_name, notification_email):
    t = Tenant(id=uuid4(), name=f"live_{uuid4().hex[:8]}", display_name=display_name,
               api_key_hash=uuid4().hex, subscription_tier="trial", subscription_status="trialing")
    db.add(t)
    db.flush()
    db.add(TenantAIConfig(id=uuid4(), tenant_id=t.id, brand_voice={}, escalation_keywords=[],
                          max_response_tokens=250, grounding_requirement=True))
    db.add(TenantCapabilities(id=uuid4(), tenant_id=t.id, can_create_tickets=True,
                              can_access_analytics=True, can_upload_knowledge=True,
                              max_conversations_per_month=1000, can_use_sales_close=True,
                              can_use_crm_notifications=True))
    db.add(TenantSalesAgentConfig(
        id=uuid4(), tenant_id=t.id, agent_enabled=True, lead_capture_enabled=True,
        meeting_booking_enabled=False, quotation_enabled=True,
        trigger_intents=["PRICING_INQUIRY", "DEMO_REQUEST", "PURCHASE_INTENT", "ENTERPRISE_PROSPECT"],
        qualification_questions=[{"key": "team_size", "question": "How large is your team?", "type": "text"}],
        pricing_tiers=[TIER], currency="NGN", annual_discount_pct=15,
        notification_email=notification_email, notify_channels={"email": True},
    ))
    return str(t.id)


db = SessionLocal()
db.bind.echo = False
out = {"with_rep": make(db, "Acme Logistics", "sales@acme-logistics.example"),
       "without_rep": make(db, "Bare Co", None)}
db.commit()
json.dump(out, sys.stdout)
