import uuid

from sqlalchemy import select

from app.db.models.knowledge import SourceStatus
from app.db.models.tenant import TenantAIConfig
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
