"""Tests for source + source_meta attribution wiring in conversation session creation."""

import uuid

import pytest

from app.db.models.conversation import ConversationSession
from app.db.models.tenant import Tenant
from app.schemas.conversation import ConversationSessionCreate
from app.schemas.explore import SourceMeta
from app.services.conversation_service import ConversationService


@pytest.fixture
def tenant(db):
    """Create a minimal tenant for session creation tests."""
    t = Tenant(
        name=f"attr_test_{uuid.uuid4().hex[:8]}",
        display_name="Attribution Test",
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"attr-{uuid.uuid4().hex[:6]}",
        is_publicly_listed=True,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_valid_source_persists(db, tenant):
    """Valid ConversationSource value is persisted as-is."""
    svc = ConversationService(db)
    session_data = ConversationSessionCreate(
        tenant_id=tenant.id,
        user_identifier="user-1",
        source="explore_browse",
        source_meta=SourceMeta(position=4, category_filter="retail_ecommerce"),
    )
    resp = svc.create_session(session_data)

    row = db.query(ConversationSession).filter_by(id=resp.id).one()
    assert row.source == "explore_browse"
    assert row.source_meta == {
        "category_filter": "retail_ecommerce",
        "search_query_present": None,
        "position": 4,
        "page": None,
    }


def test_unknown_source_coerced_to_unknown(db, tenant):
    """Unknown source value MUST coerce to 'unknown' — creation must NOT fail."""
    svc = ConversationService(db)
    session_data = ConversationSessionCreate(
        tenant_id=tenant.id,
        user_identifier="user-2",
        source="totally_made_up",
    )
    resp = svc.create_session(session_data)

    row = db.query(ConversationSession).filter_by(id=resp.id).one()
    assert row.source == "unknown"


def test_unknown_source_meta_keys_dropped(db, tenant):
    """Extra keys in source_meta are silently dropped via Pydantic's extra='ignore'."""
    svc = ConversationService(db)
    # Construct source_meta as a raw dict — SourceMeta ignores unknown keys on model_validate
    raw_meta = SourceMeta.model_validate({"position": 1, "totally_made_up": "x"})
    session_data = ConversationSessionCreate(
        tenant_id=tenant.id,
        user_identifier="user-3",
        source="widget",
        source_meta=raw_meta,
    )
    resp = svc.create_session(session_data)

    row = db.query(ConversationSession).filter_by(id=resp.id).one()
    assert row.source_meta is not None
    assert row.source_meta.get("position") == 1
    assert "totally_made_up" not in row.source_meta


def test_no_source_defaults_to_unknown(db, tenant):
    """Absence of source defaults to 'unknown' so analytics queries find every row."""
    svc = ConversationService(db)
    session_data = ConversationSessionCreate(
        tenant_id=tenant.id,
        user_identifier="user-4",
        # source intentionally omitted
    )
    resp = svc.create_session(session_data)

    row = db.query(ConversationSession).filter_by(id=resp.id).one()
    assert row.source == "unknown"


def test_visitor_bootstrap_accepts_source_without_422(client, db):
    """VisitorBootstrapRequest accepts source + source_meta fields without rejecting them."""
    t = Tenant(
        name=f"boot_{uuid.uuid4().hex[:8]}",
        display_name="Boot Test",
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"boot-{uuid.uuid4().hex[:6]}",
        is_publicly_listed=True,
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    payload = {
        "workspace_slug": t.public_slug,
        "source": "explore_browse",
        "source_meta": {"position": 4, "category_filter": "retail_ecommerce"},
    }
    resp = client.post("/api/v1/public/visitor/bootstrap", json=payload)
    # Must succeed — source/source_meta are valid fields, not extra/unknown
    assert resp.status_code == 200, resp.text


def test_visitor_bootstrap_unknown_source_accepted(client, db):
    """Unknown source on VisitorBootstrapRequest MUST NOT 422 — coerce happens downstream."""
    t = Tenant(
        name=f"boot2_{uuid.uuid4().hex[:8]}",
        display_name="Boot Test 2",
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"boot2-{uuid.uuid4().hex[:6]}",
        is_publicly_listed=True,
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    payload = {
        "workspace_slug": t.public_slug,
        "source": "totally_made_up",
    }
    resp = client.post("/api/v1/public/visitor/bootstrap", json=payload)
    assert resp.status_code == 200, resp.text
