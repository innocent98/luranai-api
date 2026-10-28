"""Platform-admin marketer onboarding: create, list, bank update, suspend, reactivate."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.encryption import hash_for_lookup
from app.db.models.referral import Marketer, MarketerStatus, ReferralReward, RewardStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.services import token_service as token_service_module
from app.services.referral import marketer_service as marketer_service_module
from app.services.referral.marketer_service import MarketerService

BASE = "/api/v1/platform/referrals"
FAKE_ACCOUNT = "0000012345"
FAKE_BANK = "000"


class FakeEmail:
    """Stands in for EmailService: records invites, can be told to fail."""

    def __init__(self, result=True, raises=False):
        self.result = result
        self.raises = raises
        self.sent: list[dict] = []

    def send_marketer_invite_email(self, **kwargs):
        if self.raises:
            raise RuntimeError("smtp down")
        self.sent.append(kwargs)
        return self.result


@pytest.fixture
def fake_email(monkeypatch):
    fake = FakeEmail()
    monkeypatch.setattr(marketer_service_module, "email_service", fake)
    return fake


@pytest.fixture
def auth(super_admin_token):
    return {"Authorization": f"Bearer {super_admin_token}"}


def _payload(**over):
    body = {
        "name": "Ada Marketer",
        "email": f"ada_{uuid4().hex[:8]}@example.com",
        "code": f"ada-{uuid4().hex[:8]}",
        "bank_code": FAKE_BANK,
        "account_number": FAKE_ACCOUNT,
        "account_name": "ADA MARKETER",
    }
    body.update(over)
    return body


def _create(client, auth, **over):
    return client.post(f"{BASE}/", json=_payload(**over), headers=auth)


def _tenant(db):
    t = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name="Ref Tenant",
        api_key_hash=uuid4().hex,
        subscription_tier="trial",
        subscription_status="trialing",
    )
    db.add(t)
    db.flush()
    return t


def _reward(db, marketer_id, status, reasons=None):
    now = datetime.now(UTC)
    r = ReferralReward(
        tenant_id=_tenant(db).id,
        marketer_id=marketer_id,
        amount_kobo=500000,
        status=status,
        converted_at=now,
        payable_after=now + timedelta(days=7),
        review_reasons=list(reasons or []),
    )
    db.add(r)
    db.flush()
    return r


# --- create -----------------------------------------------------------------------------------


def test_create_returns_marketer_and_sends_invite(pg_client, auth, fake_email):
    body = _payload(email="  Ada.Case@Example.COM ", code="Ada-Lagos")
    resp = pg_client.post(f"{BASE}/", json=body, headers=auth)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["code"] == "ada-lagos"
    assert data["email"] == "ada.case@example.com"
    assert data["status"] == "active"
    assert data["bank_last4"] == "2345"
    assert data["invite_pending"] is True
    assert len(fake_email.sent) == 1
    sent = fake_email.sent[0]
    assert sent["marketer_email"] == "ada.case@example.com"
    assert sent["invite_token"]


def test_create_stores_no_usable_password_and_normalised_email(
    pg_client, auth, fake_email, db_session
):
    resp = _create(pg_client, auth, email="Ada.Norm@Example.com")
    m = db_session.get(Marketer, resp.json()["id"])
    assert m.password_hash is None
    assert m.email == "ada.norm@example.com"
    assert m.email_hash == hash_for_lookup("ada.norm@example.com")
    assert m.created_by_admin_id is not None


def test_invite_token_is_one_time_and_carries_marketer(pg_client, auth, fake_email):
    resp = _create(pg_client, auth)
    token = fake_email.sent[0]["invite_token"]
    svc = token_service_module.token_service
    first = svc.verify_marketer_invite_token(token)
    assert str(first["marketer_id"]) == resp.json()["id"]
    assert first["email"] == resp.json()["email"]
    assert svc.verify_marketer_invite_token(token) is None


def test_create_succeeds_when_invite_email_fails(pg_client, auth, monkeypatch, db_session):
    for fake in (FakeEmail(result=False), FakeEmail(raises=True)):
        monkeypatch.setattr(marketer_service_module, "email_service", fake)
        resp = _create(pg_client, auth)
        assert resp.status_code == 201, resp.text
        assert db_session.get(Marketer, resp.json()["id"]) is not None


def test_create_rejects_tenant_user_email(pg_client, auth, fake_email, db_session):
    tenant = _tenant(db_session)
    db_session.add(
        User(
            email="owner@acme.com",
            hashed_password="x",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            tenant_id=tenant.id,
            is_active=True,
        )
    )
    db_session.flush()
    resp = _create(pg_client, auth, email="owner@acme.com")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "email_in_use"
    assert fake_email.sent == []
    assert (
        db_session.execute(select(Marketer).where(Marketer.name == "Ada Marketer")).first() is None
    )


@pytest.mark.parametrize(
    "stored,typed",
    [
        ("owner2@acme.com", "OWNER2@ACME.COM"),
        ("Owner3@acme.com", "owner3@acme.com"),
        ("OWNER4@ACME.COM", "owner4@acme.com"),
        ("owner5@acme.com", " Owner5@Acme.com "),
    ],
)
def test_tenant_email_check_catches_case_differences(
    pg_client, auth, fake_email, db_session, stored, typed
):
    tenant = _tenant(db_session)
    db_session.add(
        User(
            email=stored,
            hashed_password="x",
            full_name="Owner",
            role=UserRole.TENANT_ADMIN,
            tenant_id=tenant.id,
            is_active=True,
        )
    )
    db_session.flush()
    resp = _create(pg_client, auth, email=typed)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "email_in_use"


def test_create_rejects_existing_marketer_email_any_case(pg_client, auth, fake_email):
    assert _create(pg_client, auth, email="dup@example.com").status_code == 201
    resp = _create(pg_client, auth, email="DUP@example.com")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "email_in_use"


def test_create_duplicate_code_case_insensitive_409(pg_client, auth, fake_email):
    assert _create(pg_client, auth, code="shared-code").status_code == 201
    resp = _create(pg_client, auth, code="SHARED-CODE")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "code_taken"


def test_create_code_race_integrity_error_maps_to_409(db_session, fake_email, monkeypatch):
    """Two admins submit the same code at once: the pre-check passes, the insert collides."""
    svc = MarketerService(db_session)
    first = svc.create(
        admin_id=None,
        name="First",
        email=f"f_{uuid4().hex[:6]}@example.com",
        code="race-code",
        bank_code=FAKE_BANK,
        account_number=FAKE_ACCOUNT,
        account_name="FIRST",
    )
    assert first.code == "race-code"
    real_code_taken = MarketerService._code_taken
    calls = {"n": 0}

    def pre_check_misses_then_real(self, code):
        calls["n"] += 1
        return False if calls["n"] == 1 else real_code_taken(self, code)

    monkeypatch.setattr(MarketerService, "_code_taken", pre_check_misses_then_real)
    with pytest.raises(marketer_service_module.MarketerCodeTakenError):
        svc.create(
            admin_id=None,
            name="Second",
            email=f"s_{uuid4().hex[:6]}@example.com",
            code="RACE-CODE",
            bank_code=FAKE_BANK,
            account_number=FAKE_ACCOUNT,
            account_name="SECOND",
        )
    # the session is still usable after the failed insert
    assert db_session.execute(select(Marketer).where(Marketer.code == "race-code")).scalar_one()


@pytest.mark.parametrize("code", ["ab", "a" * 33, "admin", "a_b", "Has Space"])
def test_create_invalid_code_422(pg_client, auth, fake_email, code):
    resp = _create(pg_client, auth, code=code)
    assert resp.status_code == 422
    assert "code" in resp.text


@pytest.mark.parametrize("acct", ["123456789", "12345678901", "12345abcde", "", "0000 00000"])
def test_create_account_number_must_be_exactly_ten_digits(pg_client, auth, fake_email, acct):
    resp = _create(pg_client, auth, account_number=acct)
    assert resp.status_code == 422


@pytest.mark.parametrize("bank", ["", "12345678901"])
def test_create_bank_code_length(pg_client, auth, fake_email, bank):
    assert _create(pg_client, auth, bank_code=bank).status_code == 422


# --- responses ---------------------------------------------------------------------------------


def test_response_never_contains_full_account_number(pg_client, auth, fake_email):
    created = _create(pg_client, auth)
    mid = created.json()["id"]
    responses = [
        created,
        pg_client.get(f"{BASE}/{mid}", headers=auth),
        pg_client.get(f"{BASE}/", headers=auth),
        pg_client.patch(
            f"{BASE}/{mid}/bank",
            json={"bank_code": FAKE_BANK, "account_number": "0000099999", "account_name": "X"},
            headers=auth,
        ),
        pg_client.post(f"{BASE}/{mid}/suspend", headers=auth),
        pg_client.post(f"{BASE}/{mid}/reactivate", headers=auth),
    ]
    for r in responses:
        assert r.status_code in (200, 201), r.text
        assert FAKE_ACCOUNT not in r.text
        assert "0000099999" not in r.text
        assert "ADA MARKETER" not in r.text
        assert "password" not in r.text
        assert "account_number" not in r.text
        assert "bank_code" not in r.text
    assert "bank_last4" in responses[1].text


# --- auth --------------------------------------------------------------------------------------


def test_non_admin_token_403(pg_client, regular_user_token, fake_email):
    headers = {"Authorization": f"Bearer {regular_user_token}"}
    random_id = uuid4()
    calls = [
        pg_client.post(f"{BASE}/", json=_payload(), headers=headers),
        pg_client.get(f"{BASE}/", headers=headers),
        pg_client.get(f"{BASE}/{random_id}", headers=headers),
        pg_client.post(f"{BASE}/{random_id}/suspend", headers=headers),
    ]
    for r in calls:
        assert r.status_code in (401, 403)
    assert fake_email.sent == []


def test_no_token_rejected(pg_client):
    assert pg_client.get(f"{BASE}/").status_code in (401, 403)


# --- list / get / bank -------------------------------------------------------------------------


def test_list_get_and_unknown_id(pg_client, auth, fake_email):
    mid = _create(pg_client, auth).json()["id"]
    listing = pg_client.get(f"{BASE}/", headers=auth).json()
    assert {"items", "total", "limit", "offset"} <= set(listing)
    assert mid in [m["id"] for m in listing["items"]]
    assert pg_client.get(f"{BASE}/{mid}", headers=auth).json()["id"] == mid
    missing = pg_client.get(f"{BASE}/{uuid4()}", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "marketer_not_found"
    assert pg_client.get(f"{BASE}/not-a-uuid", headers=auth).status_code == 404


def test_list_filters_by_status(pg_client, auth, fake_email):
    mid = _create(pg_client, auth).json()["id"]
    pg_client.post(f"{BASE}/{mid}/suspend", headers=auth)
    suspended = pg_client.get(f"{BASE}/?status=suspended", headers=auth).json()
    assert mid in [m["id"] for m in suspended["items"]]
    active = pg_client.get(f"{BASE}/?status=active", headers=auth).json()
    assert mid not in [m["id"] for m in active["items"]]


def test_update_bank_details_changes_last4_and_resets_recipient(
    pg_client, auth, fake_email, db_session
):
    mid = _create(pg_client, auth).json()["id"]
    m = db_session.get(Marketer, mid)
    m.paystack_recipient_code = "RCP_old"
    db_session.flush()
    resp = pg_client.patch(
        f"{BASE}/{mid}/bank",
        json={"bank_code": "111", "account_number": "0000077777", "account_name": "NEW NAME"},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["bank_last4"] == "7777"
    db_session.refresh(m)
    assert m.account_number == "0000077777"
    assert m.bank_code == "111"
    assert m.paystack_recipient_code is None
    bad = pg_client.patch(
        f"{BASE}/{mid}/bank",
        json={"bank_code": "111", "account_number": "123", "account_name": "N"},
        headers=auth,
    )
    assert bad.status_code == 422


# --- suspend / reactivate ----------------------------------------------------------------------


def test_suspend_holds_unpaid_rewards(pg_client, auth, fake_email, db_session):
    mid = _create(pg_client, auth).json()["id"]
    other = Marketer(
        name="Other",
        email=f"o_{uuid4().hex[:6]}@example.com",
        code=f"o-{uuid4().hex[:8]}",
    )
    db_session.add(other)
    db_session.flush()
    pending = _reward(db_session, mid, RewardStatus.PENDING)
    payable = _reward(db_session, mid, RewardStatus.PAYABLE, reasons=["dup_business"])
    held_other_reason = _reward(db_session, mid, RewardStatus.HELD, reasons=["dispute"])
    batched = _reward(db_session, mid, RewardStatus.BATCHED)
    paid = _reward(db_session, mid, RewardStatus.PAID)
    void = _reward(db_session, mid, RewardStatus.VOID)
    others = _reward(db_session, other.id, RewardStatus.PENDING)

    resp = pg_client.post(f"{BASE}/{mid}/suspend", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"
    for r in (pending, payable, held_other_reason, batched, paid, void, others):
        db_session.refresh(r)
    assert pending.status == RewardStatus.HELD
    assert pending.review_reasons == ["marketer_suspended"]
    assert payable.status == RewardStatus.HELD
    assert payable.review_reasons == ["dup_business", "marketer_suspended"]
    assert held_other_reason.status == RewardStatus.HELD
    assert held_other_reason.review_reasons == ["dispute"]
    assert batched.status == RewardStatus.BATCHED
    assert paid.status == RewardStatus.PAID
    assert void.status == RewardStatus.VOID
    assert others.status == RewardStatus.PENDING


def test_suspend_is_idempotent_and_does_not_duplicate_reason(
    pg_client, auth, fake_email, db_session
):
    mid = _create(pg_client, auth).json()["id"]
    reward = _reward(db_session, mid, RewardStatus.PENDING)
    assert pg_client.post(f"{BASE}/{mid}/suspend", headers=auth).status_code == 200
    assert pg_client.post(f"{BASE}/{mid}/suspend", headers=auth).status_code == 200
    db_session.refresh(reward)
    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["marketer_suspended"]


def test_reactivate_sets_active_and_does_not_release_held_rewards(
    pg_client, auth, fake_email, db_session
):
    mid = _create(pg_client, auth).json()["id"]
    reward = _reward(db_session, mid, RewardStatus.PAYABLE)
    pg_client.post(f"{BASE}/{mid}/suspend", headers=auth)
    resp = pg_client.post(f"{BASE}/{mid}/reactivate", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert db_session.get(Marketer, mid).status == MarketerStatus.ACTIVE
    db_session.refresh(reward)
    assert reward.status == RewardStatus.HELD
    assert reward.review_reasons == ["marketer_suspended"]
    # idempotent
    assert pg_client.post(f"{BASE}/{mid}/reactivate", headers=auth).status_code == 200


def test_suspend_unknown_marketer_404(pg_client, auth):
    assert pg_client.post(f"{BASE}/{uuid4()}/suspend", headers=auth).status_code == 404
    assert pg_client.post(f"{BASE}/{uuid4()}/reactivate", headers=auth).status_code == 404


# --- route shadowing (R5) ----------------------------------------------------------------------


def test_uuid_param_does_not_shadow_literal_sibling_routes():
    from app.api.v1.endpoints.platform.referrals import router

    probe = APIRouter()
    # Mirror how later tasks add literal siblings after the parameterised routes.
    for route in router.routes:
        probe.routes.append(route)

    @probe.get("/batches")
    def _batches():
        return {"ok": True}

    matched = None
    for route in probe.routes:
        if isinstance(route, APIRoute) and route.methods and "GET" in route.methods:
            if route.path_regex.match("/batches"):
                matched = route.path
                break
    assert matched == "/batches"
    id_routes = [r for r in router.routes if isinstance(r, APIRoute) and "{marketer_id" in r.path]
    assert id_routes and all(":uuid}" in r.path for r in id_routes)


# --- invite email ------------------------------------------------------------------------------


def test_invite_email_links_to_set_password_page(monkeypatch):
    from app.core.config import settings
    from app.services.email_service import EmailService

    svc = EmailService()
    captured = {}

    def fake_send(to_email, subject, html_content, *a, **k):
        captured.update(to=to_email, subject=subject, html=html_content)
        return True

    monkeypatch.setattr(svc, "_send_email", fake_send)
    ok = svc.send_marketer_invite_email(
        marketer_email="m@example.com",
        marketer_name="Mary <b>",
        code="mary-1",
        invite_token="tok123",
    )
    assert ok is True
    assert captured["to"] == "m@example.com"
    assert f"{settings.FRONTEND_URL}/marketer/set-password?token=tok123" in captured["html"]
    assert "mary-1" in captured["html"]
    assert "Mary <b>" not in captured["html"]  # autoescaped
    assert "7 days" in captured["html"]
