"""The marketer-facing API (``/api/v1/marketer/*``).

Privacy is the binding requirement: a marketer sees their code, link, referred tenants (company
name, masked email, signup date, status, trial days, two activation hints), totals, earnings and
payout history, and NOTHING else. These tests assert the exact key set of every response object
and scan whole response bodies for anything that must never appear.

Real Postgres (``db_session`` shared with the app through ``pg_client``). Fake data only.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.core.config import settings
from app.db.models.knowledge import KnowledgeSource, SourceStatus, SourceType
from app.db.models.payment import PaymentTransaction
from app.db.models.referral import (
    AttributionSource,
    BatchStatus,
    Marketer,
    MarketerStatus,
    PayoutBatch,
    PayoutItem,
    PayoutItemStatus,
    ReferralAttribution,
    ReferralReward,
    RewardStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole
from app.services.onboarding_service import get_onboarding_status
from app.services.referral import marketer_views
from app.services.referral.config import get_referral_config
from tests.referral.test_marketer_auth import ACCESS, cookie_header, logged_in, make_marketer

BASE = "/api/v1/marketer"
RS = RewardStatus
NOW = datetime.now(UTC).replace(microsecond=0)
FIXED_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)  # month-boundary tests only

ME_KEYS = {"name", "code", "link"}
LIST_KEYS = {"items", "totals"}
ITEM_KEYS = {
    "company_name",
    "masked_email",
    "signed_up_at",
    "status",
    "trial_days_left",
    "kb_uploaded",
    "widget_installed",
    "converted_at",
}
TOTALS_KEYS = {"signed_up", "converted", "conversion_rate"}
EARNINGS_KEYS = {
    "this_month_earned_kobo",
    "pending_kobo",
    "payable_kobo",
    "paid_total_kobo",
    "payouts",
}
PAYOUT_KEYS = {"date", "amount_kobo", "batch"}

# Words that only exist on the admin side of the programme and must never reach a marketer.
FORBIDDEN_WORDS = (
    "held",
    "shared_domain",
    "shared_card",
    "shared_origin",
    "self_referral",
    "marketer_burst",
    "payment_dispute",
    "disputed",
    "review_reasons",
    "marketer_suspended",
)
PLAN_WORDS = ("professional", "enterprise", "starter", "growth", "lite")
PAYMENT_AMOUNT = 7_654_321  # a tenant payment amount (kobo) that must never be echoed


@pytest.fixture
def me(db_session) -> Marketer:
    marketer = make_marketer(db_session)
    marketer.bank_code = "000"
    marketer.account_number = "0099887766"
    marketer.account_name = "PLACEHOLDER ACCOUNT NAME"
    marketer.paystack_recipient_code = "RCP_placeholder_123"
    db_session.flush()
    return marketer


@pytest.fixture
def headers(db_session, pg_client, me):
    access, _ = logged_in(pg_client, me)
    # The login commit leaves the shared test session without its outer SAVEPOINT; restore it so
    # the SAVEPOINTs the views open nest under it (as the conftest session intends).
    db_session.begin_nested()
    return cookie_header(**{ACCESS: access})


def _referred(
    db,
    marketer,
    name,
    *,
    email=None,
    created_at=None,
    trial_ends_at=None,
    subscription_status="trialing",
    tier="trial",
    self_referral=False,
    reward_status=RS.PENDING,
    converted_at=None,
    pay=False,
):
    """A tenant (with an admin user), its attribution and optionally a reward / payment row."""
    created_at = created_at or NOW - timedelta(days=5)
    tenant = Tenant(
        name=f"t_{uuid4().hex[:8]}",
        display_name=name,
        api_key_hash=uuid4().hex,
        subscription_tier=tier,
        subscription_status=subscription_status,
        trial_ends_at=trial_ends_at if trial_ends_at is not None else NOW + timedelta(days=25),
        created_at=created_at,
    )
    db.add(tenant)
    db.flush()
    email = email or f"owner_{uuid4().hex[:8]}@acme-{uuid4().hex[:4]}.com"
    db.add(
        User(
            email=email,
            hashed_password="x",
            full_name="Owner Person",
            role=UserRole.TENANT_ADMIN,
            tenant_id=tenant.id,
            is_active=True,
            created_at=created_at,
        )
    )
    db.add(
        ReferralAttribution(
            tenant_id=tenant.id,
            marketer_id=marketer.id,
            source=AttributionSource.COOKIE,
            self_referral=self_referral,
            attributed_at=created_at,
        )
    )
    if converted_at is not None:
        converted = converted_at
        db.add(
            ReferralReward(
                tenant_id=tenant.id,
                marketer_id=marketer.id,
                amount_kobo=500_000,
                status=reward_status,
                converted_at=converted,
                payable_after=converted + timedelta(days=7),
                review_reasons=["shared_domain"] if reward_status == RS.HELD else [],
            )
        )
    if pay:
        db.add(
            PaymentTransaction(
                tenant_id=tenant.id,
                reference=f"ref_{uuid4().hex[:12]}",
                amount=PAYMENT_AMOUNT,
                tier=tier,
                billing_cycle="monthly",
                status="success",
                paid_at=NOW - timedelta(days=1),
            )
        )
    db.flush()
    return tenant, email


def _get(client, headers, path):
    return client.get(f"{BASE}{path}", headers=headers)


def _by_name(body):
    return {i["company_name"]: i for i in body["items"]}


# --- auth ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/me", "/referrals", "/earnings"])
def test_requires_a_marketer_cookie(
    db_session, pg_client, me, path, regular_user_token, super_admin_token
):
    assert pg_client.get(f"{BASE}{path}").status_code == 401
    for token in (regular_user_token, super_admin_token):
        assert (
            pg_client.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )
        assert (
            pg_client.get(f"{BASE}{path}", headers=cookie_header(access_token=token)).status_code
            == 401
        )


@pytest.mark.parametrize("path", ["/me", "/referrals", "/earnings"])
def test_suspended_marketer_is_401(db_session, pg_client, me, headers, path):
    assert pg_client.get(f"{BASE}{path}", headers=headers).status_code == 200
    me.status = MarketerStatus.SUSPENDED
    db_session.flush()
    assert pg_client.get(f"{BASE}{path}", headers=headers).status_code == 401


@pytest.mark.parametrize("path", ["/me", "/referrals", "/earnings"])
def test_marketer_routes_are_read_only(pg_client, headers, path):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(pg_client, method)(f"{BASE}{path}", headers=headers).status_code == 405


# --- /me -------------------------------------------------------------------------------------------


def test_me_has_exact_keys_and_the_link(pg_client, me, headers):
    resp = _get(pg_client, headers, "/me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == ME_KEYS
    assert body == {
        "name": me.name,
        "code": me.code,
        "link": f"{settings.REFERRAL_LINK_BASE_URL.rstrip('/')}/register?ref={me.code}",
    }
    for needle in (
        me.email,
        "0099887766",
        "PLACEHOLDER ACCOUNT NAME",
        "RCP_placeholder",
        "password",
    ):
        assert needle not in resp.text


def test_me_link_handles_a_trailing_slash_in_the_base_url(pg_client, me, headers, monkeypatch):
    monkeypatch.setattr(settings, "REFERRAL_LINK_BASE_URL", "https://luran.example/")
    assert _get(pg_client, headers, "/me").json()["link"] == (
        f"https://luran.example/register?ref={me.code}"
    )


# --- /referrals: shape and privacy -----------------------------------------------------------------


def test_referrals_list_has_only_allowed_fields(db_session, pg_client, me, headers):
    _, owner_email = _referred(
        db_session, me, "Acme Foods", tier="professional", pay=True, converted_at=NOW
    )
    _referred(db_session, me, "Beta Bakes", tier="enterprise")
    _referred(db_session, me, "Flagged Co", reward_status=RS.HELD, converted_at=NOW)
    _referred(
        db_session,
        me,
        "Selfie Ltd",
        self_referral=True,
        email="self@selfie-co.com",
        reward_status=RS.HELD,
        converted_at=NOW,
    )

    resp = _get(pg_client, headers, "/referrals")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == LIST_KEYS
    assert set(body["totals"]) == TOTALS_KEYS
    assert len(body["items"]) == 3
    for item in body["items"]:
        assert set(item) == ITEM_KEYS
    acme = _by_name(body)["Acme Foods"]
    assert acme["masked_email"] == (
        owner_email[0] + "***@" + owner_email.split("@")[1][0] + "***." + owner_email.split(".")[-1]
    )
    # nothing identifying beyond the allowed fields, anywhere in the document
    text = resp.text.lower()
    assert owner_email.lower() not in text
    assert owner_email.split("@")[0].lower() not in text
    assert "owner person" not in text
    assert "self@selfie-co.com" not in text and "selfie" not in text
    for word in PLAN_WORDS + FORBIDDEN_WORDS:
        assert word not in text, word
    assert str(PAYMENT_AMOUNT) not in text and "7654321" not in text
    for secret in (me.email, "0099887766", "placeholder account name", "rcp_placeholder"):
        assert secret.lower() not in text


def test_every_endpoint_hides_the_marketers_own_bank_fields(db_session, pg_client, me, headers):
    _referred(db_session, me, "Acme Foods", converted_at=NOW)
    for path in ("/me", "/referrals", "/earnings"):
        text = _get(pg_client, headers, path).text.lower()
        for secret in (
            me.email.lower(),
            "0099887766",
            "placeholder account name",
            "rcp_placeholder",
            "bank_code",
            "account_number",
            "password_hash",
            "paystack",
        ):
            assert secret not in text, f"{secret} leaked via {path}"


def test_marketer_sees_only_own_tenants(db_session, pg_client, me, headers):
    other = make_marketer(db_session)
    _referred(db_session, me, "Mine One")
    _referred(db_session, me, "Mine Two", converted_at=NOW)
    theirs, their_email = _referred(db_session, other, "Theirs Only", converted_at=NOW)
    theirs_reward = ReferralReward(
        tenant_id=None,
        marketer_id=other.id,
        amount_kobo=500_000,
        status=RS.PAID,
        converted_at=NOW,
        payable_after=NOW,
    )
    db_session.add(theirs_reward)
    db_session.flush()

    resp = _get(pg_client, headers, "/referrals")
    body = resp.json()

    assert set(_by_name(body)) == {"Mine One", "Mine Two"}
    assert body["totals"] == {"signed_up": 2, "converted": 1, "conversion_rate": 0.5}
    assert "Theirs Only" not in resp.text and their_email not in resp.text
    earnings = _get(pg_client, headers, "/earnings").json()
    assert earnings["pending_kobo"] == 500_000  # only my one reward
    assert earnings["paid_total_kobo"] == 0  # the other marketer's paid reward is not mine


def test_masked_email_uses_the_earliest_created_user_of_the_tenant(
    db_session, pg_client, me, headers
):
    tenant, owner_email = _referred(
        db_session,
        me,
        "Acme Foods",
        email="founder@acme-foods.com",
        created_at=NOW - timedelta(days=9),
    )
    db_session.add(
        User(
            email="agent@acme-foods.com",
            hashed_password="x",
            full_name="Agent",
            role=UserRole.SUPPORT_AGENT,
            tenant_id=tenant.id,
            is_active=True,
            created_at=NOW - timedelta(days=2),
        )
    )
    db_session.flush()
    body = _get(pg_client, headers, "/referrals").json()
    assert body["items"][0]["masked_email"] == "f***@a***.com"
    assert "agent@" not in json.dumps(body) and "a***@a***.com" not in json.dumps(body)


def test_tenant_without_users_masks_to_a_placeholder_not_an_error(
    db_session, pg_client, me, headers
):
    tenant, _ = _referred(db_session, me, "Userless Co")
    db_session.query(User).filter(User.tenant_id == tenant.id).delete()
    db_session.flush()
    resp = _get(pg_client, headers, "/referrals")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["masked_email"] == "***"


# --- /referrals: status derivation -----------------------------------------------------------------


def test_status_derivation_signed_up_converted_not_converted_and_late_conversion(
    db_session, pg_client, me, headers
):
    now = datetime.now(UTC)
    created = now - timedelta(days=10)
    _referred(
        db_session, me, "In Trial", created_at=created, trial_ends_at=now + timedelta(days=20)
    )
    _referred(
        db_session,
        me,
        "Early Payer",
        created_at=created,
        trial_ends_at=now + timedelta(days=20),
        converted_at=now - timedelta(days=2),
        reward_status=RS.PENDING,
        subscription_status="active",
    )
    _referred(
        db_session,
        me,
        "Expired Unpaid",
        created_at=now - timedelta(days=40),
        trial_ends_at=now - timedelta(days=10),
        subscription_status="trial_expired",
    )
    _referred(
        db_session,
        me,
        "Late Payer",
        created_at=now - timedelta(days=45),
        trial_ends_at=now - timedelta(days=15),
        subscription_status="active",
        converted_at=now - timedelta(days=1),
        reward_status=RS.PAYABLE,
    )

    items = _by_name(_get(pg_client, headers, "/referrals").json())

    assert items["In Trial"]["status"] == "signed_up"
    assert items["In Trial"]["trial_days_left"] in (19, 20)
    assert items["In Trial"]["converted_at"] is None
    assert items["Early Payer"]["status"] == "converted"
    assert items["Early Payer"]["trial_days_left"] is None
    assert items["Early Payer"]["converted_at"] is not None
    assert items["Expired Unpaid"]["status"] == "not_converted"
    assert items["Expired Unpaid"]["trial_days_left"] is None
    assert items["Expired Unpaid"]["converted_at"] is None
    assert items["Late Payer"]["status"] == "converted"  # turns Converted when it pays by day 90


@pytest.mark.parametrize("reward_status", [RS.PENDING, RS.HELD, RS.PAYABLE, RS.BATCHED, RS.PAID])
def test_a_reward_in_any_non_void_status_is_converted_and_held_looks_the_same(
    db_session, pg_client, me, headers, reward_status
):
    converted_at = datetime.now(UTC) - timedelta(days=3)
    _referred(db_session, me, "Payer Co", reward_status=reward_status, converted_at=converted_at)
    resp = _get(pg_client, headers, "/referrals")
    item = resp.json()["items"][0]
    assert item["status"] == "converted"
    assert datetime.fromisoformat(item["converted_at"]) == converted_at
    assert item["trial_days_left"] is None
    assert set(item) == ITEM_KEYS  # a held reward reveals nothing extra


def test_a_void_reward_shows_not_converted_even_while_the_trial_is_open(
    db_session, pg_client, me, headers
):
    now = datetime.now(UTC)
    _referred(
        db_session,
        me,
        "Void In Trial",
        created_at=now - timedelta(days=3),
        trial_ends_at=now + timedelta(days=5),
        reward_status=RS.VOID,
        converted_at=now,
    )
    _referred(
        db_session,
        me,
        "Void After Trial",
        created_at=now - timedelta(days=50),
        trial_ends_at=now - timedelta(days=20),
        subscription_status="trial_expired",
        reward_status=RS.VOID,
        converted_at=now - timedelta(days=30),
    )
    body = _get(pg_client, headers, "/referrals").json()
    items = _by_name(body)
    # R44: a void reward is "not_converted" in every case, trial still open or not
    assert items["Void In Trial"]["status"] == "not_converted"
    assert items["Void In Trial"]["converted_at"] is None
    assert items["Void In Trial"]["trial_days_left"] is None
    assert items["Void After Trial"]["status"] == "not_converted"
    assert body["totals"]["converted"] == 0


def test_self_referrals_are_hidden_from_list_and_totals(db_session, pg_client, me, headers):
    _referred(db_session, me, "Real Co", converted_at=NOW)
    _referred(
        db_session, me, "Mine Co", self_referral=True, reward_status=RS.HELD, converted_at=NOW
    )
    _referred(db_session, me, "Mine Trial Co", self_referral=True)
    body = _get(pg_client, headers, "/referrals").json()
    assert [i["company_name"] for i in body["items"]] == ["Real Co"]
    assert body["totals"] == {"signed_up": 1, "converted": 1, "conversion_rate": 1.0}


def test_a_tenant_past_the_commission_window_is_not_converted_even_if_trialing(
    db_session, pg_client, me, headers
):
    now = datetime.now(UTC)
    window = get_referral_config(db_session).commission_window_days
    _referred(
        db_session,
        me,
        "Old Trialist",
        created_at=now - timedelta(days=window + 1),
        trial_ends_at=now + timedelta(days=5),
    )
    item = _get(pg_client, headers, "/referrals").json()["items"][0]
    assert item["status"] == "not_converted" and item["trial_days_left"] is None


def test_totals_and_conversion_rate(db_session, pg_client, me, headers):
    assert _get(pg_client, headers, "/referrals").json() == {
        "items": [],
        "totals": {"signed_up": 0, "converted": 0, "conversion_rate": 0.0},
    }
    _referred(db_session, me, "A", converted_at=NOW)
    _referred(db_session, me, "B")
    _referred(db_session, me, "C")
    totals = _get(pg_client, headers, "/referrals").json()["totals"]
    assert totals == {"signed_up": 3, "converted": 1, "conversion_rate": 0.3333}


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(seconds=1), 1),
        (timedelta(days=1) - timedelta(seconds=1), 1),
        (timedelta(days=1), 1),
        (timedelta(days=1, seconds=1), 2),
        (timedelta(days=30), 30),
        (timedelta(days=29, hours=23), 30),
    ],
)
def test_trial_days_left_is_whole_days_rounded_up(db_session, me, delta, expected):
    _referred(
        db_session, me, "Boundary Co", created_at=NOW - timedelta(days=1), trial_ends_at=NOW + delta
    )
    page = marketer_views.list_referrals(db_session, me, limit=10, offset=0, now=NOW)
    item = page.items[0]
    assert item.status == "signed_up"
    assert item.trial_days_left == expected


def test_trial_that_ends_exactly_now_is_over(db_session, me):
    _referred(db_session, me, "Ends Now", created_at=NOW - timedelta(days=30), trial_ends_at=NOW)
    item = marketer_views.list_referrals(db_session, me, limit=10, offset=0, now=NOW).items[0]
    assert item.status == "not_converted" and item.trial_days_left is None


def test_trial_status_must_still_be_trialing(db_session, me):
    # An admin override can end the trial early without any payment: not in trial any more.
    _referred(
        db_session,
        me,
        "Overridden",
        trial_ends_at=NOW + timedelta(days=10),
        subscription_status="active",
    )
    item = marketer_views.list_referrals(db_session, me, limit=10, offset=0, now=NOW).items[0]
    assert item.status == "not_converted"


# --- /referrals: hints ------------------------------------------------------------------------------


def test_kb_and_widget_hints_match_onboarding_status(
    db_session, pg_client, me, headers, monkeypatch
):
    from app.utils.qdrant_client import qdrant_manager

    vectors = {}
    monkeypatch.setattr(
        qdrant_manager,
        "count_searchable_vectors",
        lambda tenant_id, source_ids: vectors.get(tenant_id, 0),
    )
    both, _ = _referred(db_session, me, "Both Done")
    kb_only, _ = _referred(db_session, me, "KB Only")
    widget_only, _ = _referred(db_session, me, "Widget Only")
    neither, _ = _referred(db_session, me, "Neither")
    unindexed, _ = _referred(db_session, me, "Source No Vectors")
    for tenant in (both, kb_only, unindexed):
        db_session.add(
            KnowledgeSource(
                tenant_id=tenant.id,
                name="faq",
                source_type=SourceType.FAQ,
                content_hash=uuid4().hex,
                status=SourceStatus.ACTIVE,
            )
        )
    vectors[both.id] = 3
    vectors[kb_only.id] = 2
    for tenant in (both, widget_only):
        tenant.widget_first_seen_at = NOW
    db_session.flush()

    items = _by_name(_get(pg_client, headers, "/referrals").json())

    assert (items["Both Done"]["kb_uploaded"], items["Both Done"]["widget_installed"]) == (
        True,
        True,
    )
    assert (items["KB Only"]["kb_uploaded"], items["KB Only"]["widget_installed"]) == (True, False)
    assert (items["Widget Only"]["kb_uploaded"], items["Widget Only"]["widget_installed"]) == (
        False,
        True,
    )
    assert (items["Neither"]["kb_uploaded"], items["Neither"]["widget_installed"]) == (False, False)
    assert items["Source No Vectors"]["kb_uploaded"] is False
    # ... exactly what the tenant's own onboarding checklist says
    for tenant, name in (
        (both, "Both Done"),
        (kb_only, "KB Only"),
        (widget_only, "Widget Only"),
        (neither, "Neither"),
        (unindexed, "Source No Vectors"),
    ):
        status = get_onboarding_status(db_session, tenant.id)
        assert items[name]["kb_uploaded"] is status.steps[0].done
        assert items[name]["widget_installed"] is status.steps[1].done


def test_hints_are_false_for_tenants_that_are_not_in_trial_and_never_computed(
    db_session, pg_client, me, headers, monkeypatch
):
    calls = []
    real = marketer_views.get_onboarding_status
    monkeypatch.setattr(
        marketer_views, "get_onboarding_status", lambda db, tid: calls.append(tid) or real(db, tid)
    )
    paid, _ = _referred(db_session, me, "Paid Co", converted_at=NOW)
    paid.widget_first_seen_at = NOW
    expired, _ = _referred(
        db_session,
        me,
        "Expired Co",
        trial_ends_at=NOW - timedelta(days=1),
        subscription_status="trial_expired",
    )
    trial, _ = _referred(db_session, me, "Trial Co")
    db_session.flush()

    items = _by_name(_get(pg_client, headers, "/referrals").json())

    assert (items["Paid Co"]["kb_uploaded"], items["Paid Co"]["widget_installed"]) == (False, False)
    assert (items["Expired Co"]["kb_uploaded"], items["Expired Co"]["widget_installed"]) == (
        False,
        False,
    )
    assert calls == [trial.id]


def test_an_onboarding_failure_degrades_to_false_without_breaking_the_page(
    db_session, pg_client, me, headers, monkeypatch
):
    def boom(db, tenant_id):
        raise RuntimeError("vector store exploded secret-detail")

    monkeypatch.setattr(marketer_views, "get_onboarding_status", boom)
    _referred(db_session, me, "Trial Co")
    resp = _get(pg_client, headers, "/referrals")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["kb_uploaded"] is False
    assert "secret-detail" not in resp.text


# --- /referrals: pagination, N+1 -------------------------------------------------------------------


def test_referrals_pagination_newest_first_and_totals_cover_everything(
    db_session, pg_client, me, headers
):
    for i in range(5):
        _referred(db_session, me, f"Co {i}", created_at=NOW - timedelta(days=20 - i))
    first = _get(pg_client, headers, "/referrals?limit=2&offset=0").json()
    second = _get(pg_client, headers, "/referrals?limit=2&offset=2").json()
    tail = _get(pg_client, headers, "/referrals?limit=2&offset=4").json()
    assert [i["company_name"] for i in first["items"]] == ["Co 4", "Co 3"]
    assert [i["company_name"] for i in second["items"]] == ["Co 2", "Co 1"]
    assert [i["company_name"] for i in tail["items"]] == ["Co 0"]
    assert first["totals"]["signed_up"] == second["totals"]["signed_up"] == 5
    assert _get(pg_client, headers, "/referrals?offset=99").json()["items"] == []


def test_referrals_limit_bounds(pg_client, headers):
    assert _get(pg_client, headers, "/referrals?limit=100").status_code == 200
    for qs in ("limit=101", "limit=500", "limit=0", "offset=-1"):
        assert _get(pg_client, headers, f"/referrals?{qs}").status_code == 422


def test_default_page_size_is_50(db_session, pg_client, me, headers):
    page = marketer_views.list_referrals(db_session, me, now=NOW)
    assert page.items == [] and page.totals.signed_up == 0
    import inspect

    assert inspect.signature(marketer_views.list_referrals).parameters["limit"].default == 50

    for i in range(55):
        _referred(db_session, me, f"Many {i:02d}", converted_at=NOW)
    body = _get(pg_client, headers, "/referrals").json()
    assert len(body["items"]) == 50
    assert body["totals"]["signed_up"] == 55  # totals still cover everything
    assert len(_get(pg_client, headers, "/referrals?offset=50").json()["items"]) == 5


class _QueryCounter:
    def __init__(self, db_session):
        self.bind = db_session.get_bind()
        self.count = 0

    def __enter__(self):
        event.listen(self.bind, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self.bind, "before_cursor_execute", self._on)

    def _on(self, *args, **kwargs):
        self.count += 1


def _queries_for(db_session, pg_client, headers):
    db_session.expire_all()
    with _QueryCounter(db_session) as counter:
        assert _get(pg_client, headers, "/referrals").status_code == 200
    return counter.count


def test_no_n_plus_one_for_converted_and_expired_tenants(db_session, pg_client, me, headers):
    for i in range(3):
        _referred(db_session, me, f"Small {i}", converted_at=NOW)
    small = _queries_for(db_session, pg_client, headers)
    for i in range(27):
        _referred(db_session, me, f"Large {i}", converted_at=NOW)
        _referred(
            db_session,
            me,
            f"Expired {i}",
            trial_ends_at=NOW - timedelta(days=1),
            subscription_status="trial_expired",
        )
    large = _queries_for(db_session, pg_client, headers)
    assert large == small  # the page is batch-loaded: same query count for 3 or 57 tenants


def test_query_count_stays_bounded_with_thirty_trial_tenants(db_session, pg_client, me, headers):
    for i in range(30):
        _referred(db_session, me, f"Trial {i}")
    # Each in-trial tenant costs a handful of onboarding queries (that service is reused as is),
    # but nothing else may grow with the number of tenants.
    assert _queries_for(db_session, pg_client, headers) < 30 * 6 + 20


# --- /earnings --------------------------------------------------------------------------------------


def _reward(db, marketer, status, amount, converted_at):
    reward = ReferralReward(
        tenant_id=None,
        marketer_id=marketer.id,
        amount_kobo=amount,
        status=status,
        converted_at=converted_at,
        payable_after=converted_at + timedelta(days=7),
    )
    db.add(reward)
    db.flush()
    return reward


def test_earnings_buckets_and_exact_keys(db_session, pg_client, me, headers):
    now = datetime.now(UTC)
    _reward(db_session, me, RS.PENDING, 100_000, now)
    _reward(db_session, me, RS.HELD, 200_000, now)
    _reward(db_session, me, RS.PAYABLE, 400_000, now)
    _reward(db_session, me, RS.BATCHED, 800_000, now)
    _reward(db_session, me, RS.PAID, 1_600_000, now)
    _reward(db_session, me, RS.VOID, 3_200_000, now)

    resp = _get(pg_client, headers, "/earnings")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == EARNINGS_KEYS
    assert body["pending_kobo"] == 300_000  # pending + held, with no sign of which is which
    assert body["payable_kobo"] == 1_200_000  # payable + batched
    assert body["paid_total_kobo"] == 1_600_000
    assert body["payouts"] == []
    # converted this month: everything but void
    assert body["this_month_earned_kobo"] == 3_100_000
    text = resp.text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in text, word


def test_held_rewards_only_show_up_as_pending(db_session, pg_client, me, headers):
    _reward(db_session, me, RS.HELD, 250_000, datetime.now(UTC) - timedelta(days=90))
    body = _get(pg_client, headers, "/earnings").json()
    assert body["pending_kobo"] == 250_000
    assert body["payable_kobo"] == 0 and body["paid_total_kobo"] == 0


def test_earnings_month_boundaries_lagos_time(db_session, me):
    # now = 15 Nov 2026: November in Africa/Lagos (UTC+1) is 31 Oct 23:00 UTC .. 30 Nov 23:00 UTC.
    cases = {
        "before_start": (datetime(2026, 10, 31, 22, 59, 59, tzinfo=UTC), 1),
        "at_start": (datetime(2026, 10, 31, 23, 0, 0, tzinfo=UTC), 2),
        "mid": (datetime(2026, 11, 15, 8, 0, 0, tzinfo=UTC), 4),
        "last_second": (datetime(2026, 11, 30, 22, 59, 59, tzinfo=UTC), 8),
        "at_end": (datetime(2026, 11, 30, 23, 0, 0, tzinfo=UTC), 16),
    }
    for converted_at, amount in cases.values():
        _reward(db_session, me, RS.PAID, amount * 1000, converted_at)
    _reward(db_session, me, RS.VOID, 999_000, datetime(2026, 11, 15, tzinfo=UTC))

    earnings = marketer_views.get_earnings(db_session, me, now=FIXED_NOW)

    assert earnings.this_month_earned_kobo == (2 + 4 + 8) * 1000
    # the same instant seen from December flips the window
    december = marketer_views.get_earnings(
        db_session, me, now=datetime(2026, 12, 20, 12, 0, tzinfo=UTC)
    )
    assert december.this_month_earned_kobo == 16 * 1000


def test_earnings_month_uses_lagos_not_utc_at_the_new_year(db_session, me):
    _reward(
        db_session, me, RS.PENDING, 5_000, datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
    )  # 00:30 Jan 1 Lagos
    in_january = marketer_views.get_earnings(
        db_session, me, now=datetime(2027, 1, 1, 0, 10, tzinfo=UTC)
    )
    in_december = marketer_views.get_earnings(
        db_session, me, now=datetime(2026, 12, 31, 12, 0, tzinfo=UTC)
    )
    assert in_january.this_month_earned_kobo == 5_000
    assert in_december.this_month_earned_kobo == 0


def _paid_item(db, marketer, label, amount, paid_at, status=PayoutItemStatus.PAID):
    batch = PayoutBatch(period_label=label, status=BatchStatus.PAID, total_kobo=amount)
    db.add(batch)
    db.flush()
    item = PayoutItem(
        batch_id=batch.id,
        marketer_id=marketer.id,
        amount_kobo=amount,
        status=status,
        paid_at=paid_at,
        transfer_reference=f"TR-{uuid4().hex[:10]}-leak-check",
        failure_reason="bank said no",
        provider_status="success",
    )
    db.add(item)
    db.flush()
    return item


def test_payout_history_newest_first_paid_items_only(db_session, pg_client, me, headers):
    other = make_marketer(db_session)
    _paid_item(db_session, me, "1 Oct 2026", 500_000, datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    _paid_item(db_session, me, "15 Oct 2026", 1_000_000, datetime(2026, 10, 15, 9, 0, tzinfo=UTC))
    _paid_item(
        db_session,
        me,
        "1 Nov 2026",
        250_000,
        datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        status=PayoutItemStatus.FAILED,
    )
    _paid_item(
        db_session, me, "15 Nov 2026", 250_000, None, status=PayoutItemStatus.AWAITING_MANUAL
    )
    _paid_item(db_session, other, "15 Oct 2026", 9_999_000, datetime(2026, 10, 16, tzinfo=UTC))

    resp = _get(pg_client, headers, "/earnings")

    body = resp.json()
    assert [(p["batch"], p["amount_kobo"]) for p in body["payouts"]] == [
        ("15 Oct 2026", 1_000_000),
        ("1 Oct 2026", 500_000),
    ]
    for payout in body["payouts"]:
        assert set(payout) == PAYOUT_KEYS
    assert datetime.fromisoformat(body["payouts"][0]["date"]) == datetime(
        2026, 10, 15, 9, 0, tzinfo=UTC
    )
    assert "leak-check" not in resp.text and "bank said no" not in resp.text
    assert "9999000" not in resp.text


def test_payout_history_is_capped_at_50(db_session, pg_client, me, headers):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(55):
        _paid_item(db_session, me, f"Batch {i:02d}", 1_000 + i, base + timedelta(days=i))
    payouts = _get(pg_client, headers, "/earnings").json()["payouts"]
    assert len(payouts) == 50
    assert payouts[0]["batch"] == "Batch 54" and payouts[-1]["batch"] == "Batch 05"


def test_earnings_for_a_new_marketer_are_all_zero(pg_client, headers):
    assert _get(pg_client, headers, "/earnings").json() == {
        "this_month_earned_kobo": 0,
        "pending_kobo": 0,
        "payable_kobo": 0,
        "paid_total_kobo": 0,
        "payouts": [],
    }


# --- hint isolation (savepoint) --------------------------------------------------------------------


def test_a_database_error_in_the_hint_code_degrades_and_totals_still_return(
    db_session, pg_client, me, headers, monkeypatch
):
    from sqlalchemy import text

    def broken(db, tenant_id):
        db.execute(text("SELECT * FROM table_that_does_not_exist_9f3"))

    monkeypatch.setattr(marketer_views, "get_onboarding_status", broken)
    _referred(db_session, me, "Trial One")
    _referred(db_session, me, "Trial Two")
    _referred(db_session, me, "Paid Co", converted_at=NOW)

    resp = _get(pg_client, headers, "/referrals")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = _by_name(body)
    for name in ("Trial One", "Trial Two"):
        assert (items[name]["kb_uploaded"], items[name]["widget_installed"]) == (False, False)
        assert items[name]["status"] == "signed_up"
    assert body["totals"] == {"signed_up": 3, "converted": 1, "conversion_rate": 0.3333}
    # the outer transaction is still usable afterwards
    assert _get(pg_client, headers, "/earnings").status_code == 200


# --- rate limit -------------------------------------------------------------------------------------


def _tight_limit(monkeypatch, max_calls):
    from app.utils import http_rate_limiter

    limiter = http_rate_limiter.marketer_portal_rate_limiter
    monkeypatch.setattr(limiter, "max_calls", max_calls)
    # The limiter singleton caches an async Redis client bound to the first test's event loop.
    monkeypatch.setattr(limiter, "_redis", None)


def test_marketer_routes_are_rate_limited_per_marketer(
    db_session, pg_client, me, headers, monkeypatch
):
    _tight_limit(monkeypatch, 3)
    other = make_marketer(db_session)
    other_access, _ = logged_in(pg_client, other)
    other_headers = cookie_header(**{ACCESS: other_access})

    assert [_get(pg_client, headers, "/me").status_code for _ in range(2)] == [200, 200]
    assert _get(pg_client, headers, "/earnings").status_code == 200  # shared budget across routes
    blocked = _get(pg_client, headers, "/referrals")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "60"
    assert _get(pg_client, headers, "/me").status_code == 429

    # another marketer (same client IP) has an untouched budget, and sees only their own data
    assert _get(pg_client, other_headers, "/me").status_code == 200
    assert _get(pg_client, other_headers, "/referrals").json()["items"] == []


def test_unauthenticated_calls_do_not_consume_a_marketers_budget(
    db_session, pg_client, me, headers, monkeypatch
):
    _tight_limit(monkeypatch, 2)
    for _ in range(5):
        assert pg_client.get(f"{BASE}/me").status_code == 401
    assert _get(pg_client, headers, "/me").status_code == 200
    assert _get(pg_client, headers, "/me").status_code == 200
    assert _get(pg_client, headers, "/me").status_code == 429
