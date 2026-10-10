"""N3 — the public widget config exposes who answers first."""

from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from app.db.models.tenant import TenantAIConfig
from tests.api.v1 import test_chat_image_send as img


def _get_config(client, tenant_id):
    key = SimpleNamespace(tenant_id=tenant_id)
    with patch("app.api.v1.endpoints.widget.APIKeyService.verify_api_key", return_value=key):
        return client.get(
            f"/api/v1/widget/public/config/{tenant_id}", headers={"X-API-Key": "pk_live_test"}
        )


def test_public_config_defaults_first_responder_to_ai(client, db):
    _, tid = img._session(db)
    r = _get_config(client, tid)
    assert r.status_code == 200, r.text
    assert r.json()["first_responder"] == "ai"


def test_public_config_exposes_human_first_responder(client, db):
    _, tid = img._session(db)
    conf = db.execute(select(TenantAIConfig).where(TenantAIConfig.tenant_id == tid)).scalar_one()
    conf.first_responder = "human"
    db.commit()
    r = _get_config(client, tid)
    assert r.status_code == 200, r.text
    assert r.json()["first_responder"] == "human"
