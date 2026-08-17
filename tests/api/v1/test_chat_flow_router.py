"""Integration tests for chat flow router behavior."""

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.endpoints import chat as chat_module
from app.db.models.conversation import Ticket
from app.db.models.flow import Flow, FlowStatus, FlowTriggerType
from app.db.models.tenant import Tenant, TenantAIConfig, TenantCapabilities


def _create_tenant_with_router_flow(db: Session) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name=f"router_test_{uuid4().hex[:8]}",
        display_name="Router Test Tenant",
        api_key_hash="test_hash",
    )
    db.add(tenant)

    ai_config = TenantAIConfig(
        id=uuid4(),
        tenant_id=tenant.id,
        brand_voice={},
        escalation_keywords=[],
        max_response_tokens=250,
        grounding_requirement=True,
    )
    db.add(ai_config)

    capabilities = TenantCapabilities(
        id=uuid4(),
        tenant_id=tenant.id,
        can_create_tickets=True,
        can_access_analytics=True,
        can_upload_knowledge=True,
        max_conversations_per_month=1000,
    )
    db.add(capabilities)

    router_flow = Flow(
        id=uuid4(),
        tenant_id=tenant.id,
        name="Support Flow",
        description="Simple support flow for router test",
        status=FlowStatus.ACTIVE,
        is_active=True,
        trigger_type=FlowTriggerType.KEYWORD,
        trigger_config={
            "keywords": ["support"],
            "match_mode": "word",
            "menu_label": "Support",
            "menu_description": "Help with common support issues",
        },
        nodes={
            "start_node_id": "support_menu",
            "nodes": [
                {
                    "id": "support_menu",
                    "type": "question",
                    "config": {
                        "question": "How can we help?",
                        "save_to": "support_choice",
                        "options": [
                            {
                                "label": "Talk to Human Agent",
                                "value": "human",
                                "next_node_id": "escalate",
                            }
                        ],
                    },
                },
                {
                    "id": "escalate",
                    "type": "escalate",
                    "config": {
                        "reason": "Customer requested human support.",
                        "message": "Connecting you to a human agent now.",
                    },
                },
            ],
            "edges": [
                {"from": "support_menu", "to": "escalate"},
            ],
        },
        priority=100,
        version=1,
    )
    db.add(router_flow)
    db.commit()
    db.refresh(tenant)
    return tenant


def test_start_router_to_flow_to_human_ticket(client: TestClient, db: Session):
    """Validate start router, flow selection, and human escalation ticket creation."""
    tenant = _create_tenant_with_router_flow(db)
    tenant_id = str(tenant.id)

    # 1) Start message returns router options
    start_resp = client.post(
        "/api/v1/chat/message",
        json={
            "tenant_id": tenant_id,
            "session_id": None,
            "message": "start",
            "user_identifier": "router_test_user",
            "metadata": {"source": "pytest"},
        },
    )
    assert start_resp.status_code == 200
    start_data = start_resp.json()
    assert start_data["message"]["meta"]["flow_router"] is True

    actions = start_data["message"]["meta"]["button_actions"]
    labels = [a["label"] for a in actions]
    assert "Support" in labels
    assert "Get Instant Help" in labels

    selected_action = next(a for a in actions if a["label"] == "Support")
    session_id = start_data["session_id"]

    # 2) Select flow from router
    flow_resp = client.post(
        "/api/v1/chat/message",
        json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            "message": "Support",
            "user_identifier": "router_test_user",
            "metadata": {"source": "pytest", "button_value": selected_action["value"]},
        },
    )
    assert flow_resp.status_code == 200
    flow_data = flow_resp.json()
    assert flow_data["message"]["content"] == "How can we help?"
    assert "Talk to Human Agent" in (flow_data["message"]["meta"].get("buttons") or [])

    # 3) Choose talk-to-human inside flow and ensure a ticket is created
    escalate_resp = client.post(
        "/api/v1/chat/message",
        json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            "message": "Talk to Human Agent",
            "user_identifier": "router_test_user",
            "metadata": {"source": "pytest"},
        },
    )
    assert escalate_resp.status_code == 200
    escalate_data = escalate_resp.json()
    assert escalate_data["should_escalate"] is True
    assert escalate_data["ticket_id"] is not None

    ticket = db.query(Ticket).filter(Ticket.id == UUID(escalate_data["ticket_id"])).first()
    assert ticket is not None


def test_re_escalating_an_already_escalated_session_reuses_the_ticket_and_rings_once(
    client: TestClient, db: Session
):
    """F1 regression: re-escalating an already-escalated session must not mint
    a second ticket or ring agents a second time.

    `escalate_session` already no-ops the session fields (reason, timestamp) on
    a second call for the same session — but nothing downstream previously
    stopped a second `create_ticket` + `_ring_agents` for the same live
    escalation. A customer typing again while an agent already owns the ticket
    would, before this fix, ring every phone on the tenant a second time and,
    90 seconds later, page every admin and mark the ticket MISSED even though
    it was being actively handled.

    Drives the real `/api/v1/chat/message` endpoint twice for the same session
    (not `escalate_session` in isolation) via the `__talk_to_human__` token,
    which reaches the escalation branch on every call regardless of session
    status.
    """
    tenant = _create_tenant_with_router_flow(db)
    tenant_id = str(tenant.id)

    enqueue = AsyncMock()
    with patch.object(chat_module, "enqueue_agent_alerts_async", enqueue):
        first = client.post(
            "/api/v1/chat/message",
            json={
                "tenant_id": tenant_id,
                "session_id": None,
                "message": "__talk_to_human__",
                "user_identifier": "reescalate_test_user",
                "metadata": {"source": "pytest"},
            },
        )
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["should_escalate"] is True
        assert first_data["ticket_id"] is not None
        session_id = first_data["session_id"]

        second = client.post(
            "/api/v1/chat/message",
            json={
                "tenant_id": tenant_id,
                "session_id": session_id,
                "message": "__talk_to_human__",
                "user_identifier": "reescalate_test_user",
                "metadata": {"source": "pytest"},
            },
        )

    assert second.status_code == 200
    second_data = second.json()
    assert second_data["should_escalate"] is True
    assert second_data["ticket_id"] == first_data["ticket_id"]

    tickets = db.query(Ticket).filter(Ticket.session_id == UUID(session_id)).all()
    assert len(tickets) == 1, f"expected exactly one ticket for the session, found {len(tickets)}"

    enqueue.assert_awaited_once()
