"""Tests for CustomerAuthService.register_customer visitor-conversation claim."""

import uuid
from datetime import UTC, datetime

from app.db.models.conversation import ConversationSession, SessionStatus
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
from app.db.models.visitor import Visitor, VisitorSource
from app.schemas.customer_auth import CustomerRegisterRequest
from app.services.customer_auth_service import CustomerAuthService


def _make_tenant(db) -> Tenant:
    t = Tenant(
        name=f"T_{uuid.uuid4().hex[:8]}",
        display_name="T",
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"t-{uuid.uuid4().hex[:6]}",
    )
    db.add(t)
    db.commit()
    return t


def _make_visitor(db, tenant_id: uuid.UUID, token: str) -> Visitor:
    now = datetime.now(UTC)
    v = Visitor(
        tenant_id=tenant_id,
        public_token=token,
        first_seen_at=now,
        last_seen_at=now,
        created_from=VisitorSource.WIDGET,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _make_session(db, *, tenant_id: uuid.UUID, visitor: Visitor) -> ConversationSession:
    s = ConversationSession(
        tenant_id=tenant_id,
        user_identifier=visitor.public_token,
        visitor_id=visitor.id,
        status=SessionStatus.ACTIVE,
        last_message_at=datetime.now(UTC),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _stub_register_side_effects(monkeypatch) -> None:
    """Bypass redis-backed token issuance + outbound email so register_customer
    runs entirely against the test DB."""
    from app.services import email_service as email_module
    from app.services import token_service as token_module

    monkeypatch.setattr(
        token_module.token_service,
        "create_customer_email_verification_token",
        lambda customer_user_id, email, expiry_hours=24: "stub-token",
    )
    monkeypatch.setattr(email_module.email_service, "send_verification_email", lambda **_: None)


def _register_payload(
    email: str, visitor_tokens: list[str] | None = None
) -> CustomerRegisterRequest:
    return CustomerRegisterRequest(
        full_name="Claim Test",
        email=email,
        password="Password123!",
        confirm_password="Password123!",
        accepted_terms=True,
        visitor_tokens=visitor_tokens,
    )


class TestClaimOnSignup:
    def test_register_claims_visitor_conversations(self, db, monkeypatch):
        _stub_register_side_effects(monkeypatch)

        tenant = _make_tenant(db)
        token = uuid.uuid4().hex
        visitor = _make_visitor(db, tenant.id, token)
        session = _make_session(db, tenant_id=tenant.id, visitor=visitor)

        assert session.customer_user_id is None  # baseline

        svc = CustomerAuthService(db)
        svc.register_customer(
            _register_payload(f"claim-{uuid.uuid4().hex[:6]}@example.com", [token])
        )

        db.refresh(session)
        customer = db.query(CustomerUser).order_by(CustomerUser.created_at.desc()).first()
        assert customer is not None
        assert session.customer_user_id == customer.id

    def test_register_without_visitor_tokens_does_not_link(self, db, monkeypatch):
        _stub_register_side_effects(monkeypatch)

        svc = CustomerAuthService(db)
        svc.register_customer(_register_payload(f"nolink-{uuid.uuid4().hex[:6]}@example.com"))

        # Nothing to assert beyond "didn't crash" — the missing-token path is
        # the common case and must succeed cleanly.

    def test_register_with_stale_token_silently_succeeds(self, db, monkeypatch):
        _stub_register_side_effects(monkeypatch)

        # Token that doesn't match any Visitor row. Signup should still
        # succeed — stale localStorage entries are user state we don't control.
        svc = CustomerAuthService(db)
        svc.register_customer(
            _register_payload(
                f"stale-{uuid.uuid4().hex[:6]}@example.com",
                [f"never-existed-{uuid.uuid4().hex}"],
            )
        )

    def test_register_does_not_overwrite_existing_customer_link(self, db, monkeypatch):
        """If a conversation already has a customer_user_id, the claim must NOT
        rewrite it — that would silently merge two different users' histories."""
        _stub_register_side_effects(monkeypatch)

        tenant = _make_tenant(db)
        token = uuid.uuid4().hex
        visitor = _make_visitor(db, tenant.id, token)
        session = _make_session(db, tenant_id=tenant.id, visitor=visitor)

        # Pre-existing customer-link on the session.
        prior_customer = CustomerUser(
            id=uuid.uuid4(),
            full_name="Prior",
            email="prior@example.com",
            email_hash=f"prior-hash-{uuid.uuid4().hex[:8]}",
            hashed_password="x",
            is_active=True,
        )
        db.add(prior_customer)
        session.customer_user_id = prior_customer.id
        db.commit()

        svc = CustomerAuthService(db)
        svc.register_customer(_register_payload(f"new-{uuid.uuid4().hex[:6]}@example.com", [token]))

        db.refresh(session)
        # Link still points at the prior customer — the new signup did NOT
        # steal the conversation.
        assert session.customer_user_id == prior_customer.id
