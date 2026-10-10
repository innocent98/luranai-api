import uuid

from sqlalchemy import select

from app.db.models.knowledge import SourceStatus
from app.db.models.tenant import TenantAIConfig
from app.services.first_responder import apply_first_responder_preset
from tests.api.v1.test_tickets_search import _auth, _make_tenant_user
from tests.services.test_first_responder import _source

AI_CONFIG = "/api/v1/tenant/ai-config"


def _admin_with_config(db, **cfg):
    user, token = _make_tenant_user(db)
    conf = db.execute(
        select(TenantAIConfig).where(TenantAIConfig.tenant_id == user.tenant_id)
    ).scalar_one_or_none()
    if conf is None:
        conf = TenantAIConfig(
            id=uuid.uuid4(),
            tenant_id=user.tenant_id,
            brand_voice={},
            escalation_keywords=[],
            max_response_tokens=250,
            grounding_requirement=True,
        )
        db.add(conf)
    for k, v in cfg.items():
        setattr(conf, k, v)
    db.commit()
    return user, token


def test_get_ai_config_reports_first_responder_and_active_sources(client, db):
    user, token = _admin_with_config(db, first_responder="human")
    _source(db, user.tenant_id, SourceStatus.ACTIVE)
    _source(db, user.tenant_id, SourceStatus.FAILED)
    r = client.get(AI_CONFIG, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_responder"] == "human"
    assert body["active_knowledge_sources"] == 1


def test_handoff_preset_forces_escalate_even_if_other_values_sent():
    out = apply_first_responder_preset(
        {"first_responder": "ai_with_handoff", "no_retrieval_action": "clarify"}
    )
    assert out["no_retrieval_action"] == "escalate"
    assert out["low_confidence_action"] == "escalate"


def test_ai_and_human_do_not_touch_knobs():
    assert apply_first_responder_preset({"first_responder": "ai"}) == {"first_responder": "ai"}
    assert apply_first_responder_preset(
        {"first_responder": "human", "no_retrieval_action": "answer"}
    ) == {"first_responder": "human", "no_retrieval_action": "answer"}


def test_put_handoff_then_ai_keeps_escalate(client, db):
    _, token = _admin_with_config(db)
    r = client.put(
        AI_CONFIG,
        json={"first_responder": "ai_with_handoff", "no_retrieval_action": "clarify"},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["no_retrieval_action"] == "escalate"
    assert r.json()["low_confidence_action"] == "escalate"
    r = client.put(AI_CONFIG, json={"first_responder": "ai"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["first_responder"] == "ai"
    assert body["no_retrieval_action"] == "escalate"
    assert body["low_confidence_action"] == "escalate"


def test_stored_handoff_cannot_be_unlocked_by_knob_only_put(client, db):
    _, token = _admin_with_config(
        db,
        first_responder="ai_with_handoff",
        no_retrieval_action="escalate",
        low_confidence_action="escalate",
    )
    r = client.put(AI_CONFIG, json={"no_retrieval_action": "answer"}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["no_retrieval_action"] == "escalate"


def test_put_rejects_unknown_mode(client, db):
    _, token = _admin_with_config(db)
    r = client.put(AI_CONFIG, json={"first_responder": "robot"}, headers=_auth(token))
    assert r.status_code == 422
