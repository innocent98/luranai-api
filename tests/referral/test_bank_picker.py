"""Referral admin bank picker: Paystack bank list and account-name resolution (fake Paystack).

Nothing here calls Paystack: every test injects an ``httpx.MockTransport`` through the
``get_paystack_service`` dependency. The real Paystack response shapes are therefore the shapes
documented by Paystack, not shapes captured from a live call.
"""

import json
from uuid import uuid4

import httpx
import pytest
from loguru import logger

from app.api.v1.endpoints.platform import referrals as referrals_endpoint
from app.core.security import create_access_token
from app.main import app
from app.services.paystack_service import PaystackService
from app.services.referral import banks as banks_module
from app.utils import http_rate_limiter

BASE = "/api/v1/platform/referrals"
FAKE_KEY = "sk_test_FAKEKEYDONOTUSE"
ACCOUNT = "0123456789"


def _bank(name, code, **over):
    row = {
        "id": 1,
        "name": name,
        "slug": name.lower().replace(" ", "-"),
        "code": code,
        "longcode": f"{code}0000",
        "gateway": None,
        "pay_with_bank": False,
        "supports_transfer": True,
        "active": True,
        "country": "Nigeria",
        "currency": "NGN",
        "type": "nuban",
        "is_deleted": False,
    }
    row.update(over)
    return row


BANKS = [
    _bank("Zenith Bank", "057"),
    _bank("access bank", "044"),
    _bank("Closed Bank", "999", active=False),
    _bank("Gone Bank", "998", is_deleted=True),
    _bank("Some Wallet", "997", type="mobile_money"),
    _bank("Guaranty Trust Bank", "058"),
    _bank("Zenith Bank duplicate", "057"),
]


class FakePaystack:
    """Counts calls and answers like Paystack; ``mode`` switches the behaviour."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.banks = BANKS
        self.pages: list[dict] | None = None  # explicit cursor pages, when set
        self.bank_status = 200
        self.resolve_status = 200
        self.resolve_body: dict | None = None
        self.raise_exc: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        if self.raise_exc is not None:
            raise self.raise_exc
        path = request.url.path
        if path == "/bank":
            if self.bank_status != 200:
                return httpx.Response(self.bank_status, json={"status": False, "message": "boom"})
            if self.pages is not None:
                cursor = request.url.params.get("next")
                index = 0 if cursor is None else int(cursor)
                page = self.pages[index]
                nxt = str(index + 1) if index + 1 < len(self.pages) else None
                return httpx.Response(
                    200,
                    json={
                        "status": True,
                        "message": "Banks retrieved",
                        "data": page,
                        "meta": {"next": nxt, "previous": None, "perPage": 100},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "message": "Banks retrieved",
                    "data": self.banks,
                    "meta": {"next": None, "previous": None, "perPage": 100},
                },
            )
        assert path == "/bank/resolve"
        if self.resolve_body is not None:
            return httpx.Response(self.resolve_status, json=self.resolve_body)
        if self.resolve_status == 200:
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "message": "Account number resolved",
                    "data": {
                        "account_number": request.url.params["account_number"],
                        "account_name": "ADA MARKETER",
                        "bank_id": 9,
                    },
                },
            )
        return httpx.Response(self.resolve_status, json={"status": False, "message": "no"})

    def bank_calls(self):
        return [c for c in self.calls if c.url.path == "/bank"]

    def resolve_calls(self):
        return [c for c in self.calls if c.url.path == "/bank/resolve"]


@pytest.fixture(autouse=True)
def _clean_cache():
    banks_module.clear_cache()
    yield
    banks_module.clear_cache()


@pytest.fixture
def fake_paystack(db_session):
    fake = FakePaystack()

    def provide():
        service = PaystackService(db_session, transport=httpx.MockTransport(fake))
        service.secret_key = FAKE_KEY
        service.headers = {"Authorization": f"Bearer {FAKE_KEY}"}
        return service

    app.dependency_overrides[referrals_endpoint.get_paystack_service] = provide
    yield fake
    app.dependency_overrides.pop(referrals_endpoint.get_paystack_service, None)


@pytest.fixture
def auth(super_admin_token):
    return {"Authorization": f"Bearer {super_admin_token}"}


@pytest.fixture
def plain_admin_headers(db_session):
    from app.db.models.platform_admin import PlatformAdmin, PlatformAdminRole

    admin = PlatformAdmin(
        email=f"plain_{uuid4().hex[:8]}@testdomain.com",
        hashed_password="x",
        full_name="Plain Admin",
        role=PlatformAdminRole.ADMIN,
        is_active=True,
    )
    db_session.add(admin)
    db_session.flush()
    token = create_access_token(
        subject=str(admin.id),
        additional_claims={"role": admin.role.value, "admin_type": "platform_admin"},
    )
    return {"Authorization": f"Bearer {token}"}


def _resolve(client, headers, bank_code="058", account_number=ACCOUNT):
    return client.post(
        f"{BASE}/banks/resolve",
        json={"bank_code": bank_code, "account_number": account_number},
        headers=headers,
    )


# --- bank list ----------------------------------------------------------------------------------


def test_bank_list_is_sorted_active_and_name_code_only(pg_client, auth, fake_paystack):
    res = pg_client.get(f"{BASE}/banks", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["banks"] == [
        {"name": "access bank", "code": "044"},
        {"name": "Guaranty Trust Bank", "code": "058"},
        {"name": "Zenith Bank", "code": "057"},
    ]
    assert body["stale"] is False
    assert body["test_mode"] is True  # the fake key is sk_test_
    assert FAKE_KEY not in res.text
    request = fake_paystack.bank_calls()[0]
    assert request.url.params["country"] == "nigeria"


def test_bank_list_follows_cursor_pages(pg_client, auth, fake_paystack):
    fake_paystack.pages = [[_bank("Bank A", "001")], [_bank("Bank B", "002")]]
    body = pg_client.get(f"{BASE}/banks", headers=auth).json()
    assert [b["code"] for b in body["banks"]] == ["001", "002"]
    assert len(fake_paystack.bank_calls()) == 2


def test_bank_list_is_cached_for_24_hours(pg_client, auth, fake_paystack, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(banks_module, "_now", lambda: clock["t"])
    pg_client.get(f"{BASE}/banks", headers=auth)
    clock["t"] += 23 * 3600
    pg_client.get(f"{BASE}/banks", headers=auth)
    assert len(fake_paystack.bank_calls()) == 1
    clock["t"] += 2 * 3600  # 25 h after the first fetch
    res = pg_client.get(f"{BASE}/banks", headers=auth)
    assert res.status_code == 200
    assert len(fake_paystack.bank_calls()) == 2


def test_bank_list_serves_stale_copy_when_paystack_fails(
    pg_client, auth, fake_paystack, monkeypatch
):
    clock = {"t": 1000.0}
    monkeypatch.setattr(banks_module, "_now", lambda: clock["t"])
    pg_client.get(f"{BASE}/banks", headers=auth)
    clock["t"] += 25 * 3600
    fake_paystack.bank_status = 503
    res = pg_client.get(f"{BASE}/banks", headers=auth)
    assert res.status_code == 200
    assert res.json()["stale"] is True
    assert len(res.json()["banks"]) == 3


def test_bank_list_stale_copy_expires_after_seven_days(pg_client, auth, fake_paystack, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(banks_module, "_now", lambda: clock["t"])
    pg_client.get(f"{BASE}/banks", headers=auth)
    clock["t"] += 8 * 24 * 3600
    fake_paystack.bank_status = 503
    assert pg_client.get(f"{BASE}/banks", headers=auth).status_code == 503


@pytest.mark.parametrize("failure", ["status_500", "status_401", "status_429", "timeout", "conn"])
def test_bank_list_paystack_down_is_503_with_code(pg_client, auth, fake_paystack, failure):
    if failure.startswith("status_"):
        fake_paystack.bank_status = int(failure.split("_")[1])
    elif failure == "timeout":
        fake_paystack.raise_exc = httpx.ReadTimeout("slow")
    else:
        fake_paystack.raise_exc = httpx.ConnectError("down")
    res = pg_client.get(f"{BASE}/banks", headers=auth)
    assert res.status_code == 503
    detail = res.json()["detail"]
    assert detail["code"] == "bank_provider_unavailable"
    assert "manually" in detail["message"]
    assert FAKE_KEY not in res.text


def test_failed_fetch_is_not_cached(pg_client, auth, fake_paystack):
    fake_paystack.bank_status = 503
    assert pg_client.get(f"{BASE}/banks", headers=auth).status_code == 503
    fake_paystack.bank_status = 200
    assert pg_client.get(f"{BASE}/banks", headers=auth).status_code == 200


def test_empty_bank_list_is_treated_as_unavailable(pg_client, auth, fake_paystack):
    fake_paystack.banks = [_bank("Closed", "1", active=False)]
    res = pg_client.get(f"{BASE}/banks", headers=auth)
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "bank_provider_unavailable"


def test_live_key_reports_test_mode_false(pg_client, auth, db_session):
    fake = FakePaystack()

    def provide():
        service = PaystackService(db_session, transport=httpx.MockTransport(fake))
        service.headers = {"Authorization": f"Bearer {FAKE_KEY}"}
        service.secret_key = "sk_live_FAKE"
        return service

    app.dependency_overrides[referrals_endpoint.get_paystack_service] = provide
    try:
        body = pg_client.get(f"{BASE}/banks", headers=auth).json()
    finally:
        app.dependency_overrides.pop(referrals_endpoint.get_paystack_service, None)
    assert body["test_mode"] is False
    assert "sk_live" not in json.dumps(body)


# --- resolve ------------------------------------------------------------------------------------


def test_resolve_returns_the_account_name_only(pg_client, auth, fake_paystack):
    res = _resolve(pg_client, auth)
    assert res.status_code == 200
    assert res.json() == {"account_name": "ADA MARKETER"}
    request = fake_paystack.resolve_calls()[0]
    assert request.method == "GET"
    assert request.url.params["account_number"] == ACCOUNT
    assert request.url.params["bank_code"] == "058"


@pytest.mark.parametrize("paystack_status", [400, 404, 422])
def test_resolve_unknown_account_is_422_account_not_resolved(
    pg_client, auth, fake_paystack, paystack_status
):
    fake_paystack.resolve_status = paystack_status
    fake_paystack.resolve_body = {
        "status": False,
        "message": f"Could not resolve account name {ACCOUNT}. Check parameters or try again.",
    }
    res = _resolve(pg_client, auth)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "account_not_resolved"
    assert ACCOUNT not in res.text  # Paystack's message is never relayed


@pytest.mark.parametrize("failure", ["status_500", "status_401", "status_403", "status_429"])
def test_resolve_provider_failures_are_503(pg_client, auth, fake_paystack, failure):
    fake_paystack.resolve_status = int(failure.split("_")[1])
    res = _resolve(pg_client, auth)
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "bank_provider_unavailable"


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
def test_resolve_transport_errors_are_503(pg_client, auth, fake_paystack, exc):
    fake_paystack.raise_exc = exc
    res = _resolve(pg_client, auth)
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "bank_provider_unavailable"


def test_resolve_200_without_a_name_is_unavailable_not_a_crash(pg_client, auth, fake_paystack):
    fake_paystack.resolve_body = {"status": True, "message": "ok", "data": {}}
    res = _resolve(pg_client, auth)
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "bank_provider_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"bank_code": "", "account_number": ACCOUNT},
        {"bank_code": "x" * 11, "account_number": ACCOUNT},
        {"bank_code": "058", "account_number": "12345"},
        {"bank_code": "058", "account_number": "abcdefghij"},
        {"bank_code": "058"},
        {"bank_code": "058", "account_number": ACCOUNT, "extra": 1},
    ],
)
def test_resolve_rejects_bad_input_without_calling_paystack(pg_client, auth, fake_paystack, body):
    res = pg_client.post(f"{BASE}/banks/resolve", json=body, headers=auth)
    assert res.status_code == 422
    assert fake_paystack.calls == []


def test_resolve_never_logs_the_account_number_or_key(pg_client, auth, fake_paystack):
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        _resolve(pg_client, auth)
        fake_paystack.resolve_status = 422
        _resolve(pg_client, auth)
        fake_paystack.resolve_status = 500
        _resolve(pg_client, auth)
        fake_paystack.raise_exc = httpx.ReadTimeout("slow")
        _resolve(pg_client, auth)
        pg_client.get(f"{BASE}/banks", headers=auth)
    finally:
        logger.remove(sink)
    joined = "\n".join(lines)
    assert ACCOUNT not in joined
    assert FAKE_KEY not in joined


# --- auth + rate limit --------------------------------------------------------------------------


def test_banks_endpoints_need_a_platform_admin(pg_client, regular_user_token, fake_paystack):
    headers = {"Authorization": f"Bearer {regular_user_token}"}
    calls = [
        pg_client.get(f"{BASE}/banks", headers=headers),
        pg_client.post(f"{BASE}/banks/resolve", json={}, headers=headers),
        pg_client.get(f"{BASE}/banks"),
        pg_client.post(f"{BASE}/banks/resolve", json={}),
    ]
    assert [r.status_code for r in calls] == [401] * 4
    assert fake_paystack.calls == []


def test_plain_admin_can_list_banks_but_not_resolve(pg_client, plain_admin_headers, fake_paystack):
    assert pg_client.get(f"{BASE}/banks", headers=plain_admin_headers).status_code == 200
    res = _resolve(pg_client, plain_admin_headers)
    assert res.status_code == 403
    assert fake_paystack.resolve_calls() == []


def test_resolve_is_rate_limited_per_admin(pg_client, auth, fake_paystack):
    limiter = http_rate_limiter.bank_resolve_rate_limiter
    limiter._redis = None  # a connection cached by an earlier test's event loop would fail open
    limit = limiter.max_calls
    codes = [_resolve(pg_client, auth).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [200] * limit
    assert codes[limit] == 429
    assert len(fake_paystack.resolve_calls()) == limit
    limiter._redis = None
