"""Marketer authentication and identity isolation.

Security-critical: a marketer session must never reach tenant or platform-admin data, and tenant /
platform / customer tokens must never satisfy a marketer dependency.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from uuid import uuid4

import pytest
from fastapi import Depends
from jose import jwt
from sqlalchemy import select

from app.api.deps import get_current_marketer
from app.core.config import settings
from app.core.security import create_access_token, create_refresh_token, get_password_hash
from app.db.models.referral import Marketer, MarketerStatus
from app.db.models.refresh_token import RefreshToken
from app.main import app
from app.services import token_service as token_service_module
from app.services.referral import marketer_auth_service as auth_service_module
from app.utils import http_rate_limiter

AUTH = "/api/v1/auth/marketer"
PROBE = "/api/v1/__marketer_probe"
PASSWORD = "Fake-Placeholder-Pass-1"  # placeholder, not a real credential
NEW_PASSWORD = "Fake-Placeholder-Pass-2"  # placeholder, not a real credential
ACCESS = "marketer_access_token"
REFRESH = "marketer_refresh_token"
INVALID_CREDENTIALS = {
    "detail": {"code": "invalid_credentials", "message": "Incorrect email or password"}
}
INVALID_TOKEN = {
    "detail": {
        "code": "invalid_or_expired_token",
        "message": "This link is invalid or has expired.",
    }
}


class FakeEmail:
    """Stands in for EmailService: records marketer password-reset emails."""

    def __init__(self, raises=False):
        self.raises = raises
        self.sent: list[dict] = []

    def send_marketer_password_reset_email(self, **kwargs):
        if self.raises:
            raise RuntimeError("smtp down")
        self.sent.append(kwargs)
        return True


@pytest.fixture
def fake_email(monkeypatch):
    fake = FakeEmail()
    monkeypatch.setattr(auth_service_module, "email_service", fake)
    return fake


@pytest.fixture
def probe_route():
    """A throwaway ``get_current_marketer``-protected route (real /marketer/* routes: Task 9)."""

    def _probe(marketer: Marketer = Depends(get_current_marketer)):
        return {"marketer_id": str(marketer.id)}

    app.add_api_route(PROBE, _probe, methods=["GET"])
    yield PROBE
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) != PROBE]


def make_marketer(db, *, password=PASSWORD, status=MarketerStatus.ACTIVE, email=None) -> Marketer:
    marketer = Marketer(
        name="Ada Marketer",
        email=email or f"ada_{uuid4().hex[:8]}@example.com",
        password_hash=get_password_hash(password) if password else None,
        code=f"ada-{uuid4().hex[:8]}",
        status=status,
    )
    db.add(marketer)
    db.flush()
    return marketer


def login(client, marketer_or_email, password=PASSWORD):
    email = marketer_or_email if isinstance(marketer_or_email, str) else marketer_or_email.email
    return client.post(f"{AUTH}/login", json={"email": email, "password": password})


def cookies_from(resp) -> dict[str, dict]:
    """Parse every Set-Cookie header into {name: {"value": ..., "path": ..., ...}}."""
    out = {}
    for header in resp.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        for name, morsel in jar.items():
            out[name] = {
                "value": morsel.value,
                "path": morsel["path"],
                "httponly": bool(morsel["httponly"]),
                "secure": bool(morsel["secure"]),
                "max-age": morsel["max-age"],
            }
    return out


def cookie_header(**cookies) -> dict:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


def logged_in(client, marketer):
    resp = login(client, marketer)
    assert resp.status_code == 200, resp.text
    jar = cookies_from(resp)
    return jar[ACCESS]["value"], jar[REFRESH]["value"]


# --- login -----------------------------------------------------------------------------------


def test_login_sets_only_marketer_cookies(pg_client, db_session):
    marketer = make_marketer(db_session)
    resp = login(pg_client, marketer)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"role": "marketer", "redirect": "/marketer"}

    jar = cookies_from(resp)
    assert set(jar) == {ACCESS, REFRESH}  # never the tenant / platform / customer cookie names
    assert jar[ACCESS]["path"] == "/"
    assert jar[REFRESH]["path"] == "/api/v1/auth/marketer"
    for cookie in jar.values():
        assert cookie["httponly"] and cookie["secure"]

    # Tokens are cookie-only: nothing token-shaped in the body.
    assert jar[ACCESS]["value"] not in resp.text
    assert jar[REFRESH]["value"] not in resp.text

    claims = jwt.decode(jar[ACCESS]["value"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert claims["admin_type"] == "marketer"
    assert claims["sub"] == str(marketer.id)
    # No PII and no claim a tenant / platform verifier could mistake for its own.
    assert not {"email", "role", "tenant_id", "auth_type"} & set(claims)


def test_login_stores_only_refresh_hash(pg_client, db_session):
    marketer = make_marketer(db_session)
    _, raw_refresh = logged_in(pg_client, marketer)
    rows = db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == marketer.id)
    ).scalars()
    rows = list(rows)
    assert len(rows) == 1
    assert rows[0].user_type == "marketer"
    assert rows[0].token_hash == hashlib.sha256(raw_refresh.encode()).hexdigest()
    assert raw_refresh not in rows[0].token_hash


def test_login_normalises_email(pg_client, db_session):
    marketer = make_marketer(db_session, email="ada.case@example.com")
    resp = login(pg_client, "  ADA.Case@Example.COM ")
    assert resp.status_code == 200, resp.text
    assert set(cookies_from(resp)) == {ACCESS, REFRESH}
    claims = jwt.decode(
        cookies_from(resp)[ACCESS]["value"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert claims["sub"] == str(marketer.id)


def test_suspended_marketer_cannot_login(pg_client, db_session):
    marketer = make_marketer(db_session, status=MarketerStatus.SUSPENDED)
    resp = login(pg_client, marketer)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "account_suspended"
    assert cookies_from(resp) == {}

    # Wrong password on a suspended account must not reveal the suspension.
    resp = login(pg_client, marketer, password="Wrong-Placeholder-1")
    assert resp.status_code == 401
    assert resp.json() == INVALID_CREDENTIALS


def test_marketer_without_password_logs_in_as_invalid_credentials(pg_client, db_session):
    marketer = make_marketer(db_session, password=None)
    resp = login(pg_client, marketer, password="anything-at-all-1")
    assert resp.status_code == 401
    assert resp.json() == INVALID_CREDENTIALS
    assert cookies_from(resp) == {}


def test_unknown_email_and_wrong_password_identical_response(pg_client, db_session, monkeypatch):
    marketer = make_marketer(db_session)

    checked = []
    real_verify = auth_service_module.verify_password

    def spy(plain, hashed):
        checked.append(hashed)
        return real_verify(plain, hashed)

    monkeypatch.setattr(auth_service_module, "verify_password", spy)

    wrong = login(pg_client, marketer, password="Wrong-Placeholder-1")
    unknown = login(
        pg_client, f"nobody_{uuid4().hex[:8]}@example.com", password="Wrong-Placeholder-1"
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == INVALID_CREDENTIALS
    assert wrong.headers.get("www-authenticate") == unknown.headers.get("www-authenticate")
    assert wrong.headers["content-length"] == unknown.headers["content-length"]
    assert cookies_from(wrong) == cookies_from(unknown) == {}

    # A password hash check ran for BOTH (dummy hash for the unknown email) - no timing oracle.
    assert len(checked) == 2
    assert all(h and h.startswith("$2") for h in checked)


def _tight_limit(monkeypatch, max_calls: int) -> None:
    limiter = http_rate_limiter.marketer_auth_rate_limiter
    monkeypatch.setattr(limiter, "max_calls", max_calls)
    # The limiter singleton caches an async Redis client bound to the first test's event loop.
    monkeypatch.setattr(limiter, "_redis", None)


def test_login_rate_limited(pg_client, db_session, monkeypatch):
    _tight_limit(monkeypatch, 3)
    marketer = make_marketer(db_session)
    statuses = [
        login(pg_client, marketer, password="Wrong-Placeholder-1").status_code for _ in range(4)
    ]
    assert statuses == [401, 401, 401, 429]


def test_forgot_and_set_password_are_rate_limited(pg_client, db_session, monkeypatch, fake_email):
    _tight_limit(monkeypatch, 2)
    body = {"email": "nobody@example.com"}
    assert pg_client.post(f"{AUTH}/forgot-password", json=body).status_code == 200
    assert pg_client.post(f"{AUTH}/forgot-password", json=body).status_code == 200
    assert pg_client.post(f"{AUTH}/forgot-password", json=body).status_code == 429
    # Counter is shared per IP across the three endpoints.
    resp = pg_client.post(f"{AUTH}/set-password", json={"token": "x", "new_password": PASSWORD})
    assert resp.status_code == 429


# --- get_current_marketer --------------------------------------------------------------------


def test_marketer_access_cookie_reaches_marketer_route(pg_client, db_session, probe_route):
    marketer = make_marketer(db_session)
    access, _ = logged_in(pg_client, marketer)
    resp = pg_client.get(probe_route, headers=cookie_header(**{ACCESS: access}))
    assert resp.status_code == 200
    assert resp.json() == {"marketer_id": str(marketer.id)}


def test_marketer_route_ignores_bearer_header(pg_client, db_session, probe_route):
    """Cookie-only by design: a Bearer header never authenticates a marketer endpoint."""
    marketer = make_marketer(db_session)
    access, _ = logged_in(pg_client, marketer)
    resp = pg_client.get(probe_route, headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 401


def test_marketer_route_without_token_is_401(pg_client, probe_route):
    assert pg_client.get(probe_route).status_code == 401
    garbage = cookie_header(**{ACCESS: "not-a-jwt"})
    assert pg_client.get(probe_route, headers=garbage).status_code == 401


def test_tenant_and_platform_tokens_rejected_by_marketer_endpoints(
    pg_client, probe_route, regular_user_token, super_admin_token
):
    for name, token in (("tenant", regular_user_token), ("platform", super_admin_token)):
        as_cookie = pg_client.get(probe_route, headers=cookie_header(**{ACCESS: token}))
        assert as_cookie.status_code == 401, name
        # Also when presented in the cookie/header the tenant stack would read.
        as_tenant_cookie = pg_client.get(probe_route, headers=cookie_header(access_token=token))
        assert as_tenant_cookie.status_code == 401, name
        as_bearer = pg_client.get(probe_route, headers={"Authorization": f"Bearer {token}"})
        assert as_bearer.status_code == 401, name


def test_marketer_route_rejects_forged_expired_and_other_identity_tokens(
    pg_client, db_session, probe_route
):
    marketer = make_marketer(db_session)
    claims = {"exp": datetime.now(UTC) + timedelta(minutes=5), "sub": str(marketer.id)}

    forged = jwt.encode({**claims, "admin_type": "marketer"}, "not-the-secret", algorithm="HS256")
    expired = create_access_token(
        str(marketer.id),
        expires_delta=timedelta(minutes=-1),
        additional_claims={"admin_type": "marketer"},
    )
    customer = create_access_token(str(marketer.id), additional_claims={"auth_type": "customer"})
    no_type = create_access_token(str(marketer.id))
    admin_type_platform = create_access_token(
        str(marketer.id), additional_claims={"admin_type": "platform_admin", "role": "super_admin"}
    )
    for token in (forged, expired, customer, no_type, admin_type_platform):
        resp = pg_client.get(probe_route, headers=cookie_header(**{ACCESS: token}))
        assert resp.status_code == 401


def test_marketer_token_for_unknown_marketer_is_401(pg_client, probe_route):
    token = create_access_token(str(uuid4()), additional_claims={"admin_type": "marketer"})
    assert pg_client.get(probe_route, headers=cookie_header(**{ACCESS: token})).status_code == 401


def test_suspended_marketers_live_session_stops_working(pg_client, db_session, probe_route):
    marketer = make_marketer(db_session)
    access, refresh = logged_in(pg_client, marketer)
    assert pg_client.get(probe_route, headers=cookie_header(**{ACCESS: access})).status_code == 200

    marketer.status = MarketerStatus.SUSPENDED
    db_session.flush()

    resp = pg_client.get(probe_route, headers=cookie_header(**{ACCESS: access}))
    assert resp.status_code == 401
    resp = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: refresh}))
    assert resp.status_code == 401
    assert cookies_from(resp) == {}


# --- isolation: marketer token vs tenant / platform / customer dependencies ------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/me",  # get_current_user
        "/api/v1/platform/referrals/",  # get_current_super_admin / platform admin
        "/api/v1/platform/auth/me",  # get_current_platform_admin
        "/api/v1/customer/auth/me",  # get_current_customer
    ],
)
def test_marketer_token_rejected_by_tenant_and_platform_dependencies(pg_client, db_session, path):
    expected_detail = (
        "Could not validate customer credentials"
        if path.startswith("/api/v1/customer/")
        else "Could not validate credentials"
    )
    marketer = make_marketer(db_session)
    access, _ = logged_in(pg_client, marketer)

    attempts = (
        cookie_header(access_token=access),  # tenant cookie name
        cookie_header(**{ACCESS: access}),  # its own cookie
        cookie_header(customer_access_token=access),
        {"Authorization": f"Bearer {access}"},
    )
    for headers in attempts:
        resp = pg_client.get(path, headers=headers)
        # Strictly 401 "no valid credentials": a 403 would mean the token got past authentication.
        assert resp.status_code == 401, (path, headers, resp.text)
        assert resp.json() == {"detail": expected_detail}, (path, headers)


def test_marketer_token_rejected_by_tenant_admin_dependency(pg_client, db_session):
    """get_current_tenant_admin / get_current_user_or_platform_admin both refuse it."""
    marketer = make_marketer(db_session)
    access, _ = logged_in(pg_client, marketer)

    from fastapi import Depends as _Depends  # noqa: F401 - route registered below

    from app.api.deps import get_current_tenant_admin, get_current_user_or_platform_admin

    paths = {
        "/api/v1/__probe_tenant_admin": get_current_tenant_admin,
        "/api/v1/__probe_dual": get_current_user_or_platform_admin,
    }
    for path, dep in paths.items():

        def _route(_ctx=Depends(dep)):  # noqa: B008
            return {"ok": True}

        app.add_api_route(path, _route, methods=["GET"])
    try:
        for path in paths:
            for headers in (
                cookie_header(access_token=access),
                {"Authorization": f"Bearer {access}"},
            ):
                resp = pg_client.get(path, headers=headers)
                assert resp.status_code == 401, (path, resp.text)
                assert resp.json() == {"detail": "Could not validate credentials"}, path
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) not in paths
        ]


def test_marketer_token_rejected_by_platform_admin_dependency(
    pg_client, db_session, super_admin_token
):
    marketer = make_marketer(db_session)
    access, _ = logged_in(pg_client, marketer)
    # Sanity: a real platform admin token does get through the same route.
    ok = pg_client.get(
        "/api/v1/platform/referrals/", headers={"Authorization": f"Bearer {super_admin_token}"}
    )
    assert ok.status_code == 200
    for headers in (cookie_header(access_token=access), {"Authorization": f"Bearer {access}"}):
        resp = pg_client.get("/api/v1/platform/referrals/", headers=headers)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Could not validate credentials"}


# --- refresh / logout ------------------------------------------------------------------------


def test_refresh_rotates_and_old_token_is_dead(pg_client, db_session, probe_route):
    marketer = make_marketer(db_session)
    _, refresh1 = logged_in(pg_client, marketer)

    resp = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: refresh1}))
    assert resp.status_code == 200, resp.text
    jar = cookies_from(resp)
    assert set(jar) == {ACCESS, REFRESH}
    assert jar[REFRESH]["value"] != refresh1
    assert jar[ACCESS]["value"] not in resp.text and jar[REFRESH]["value"] not in resp.text
    assert (
        pg_client.get(
            probe_route, headers=cookie_header(**{ACCESS: jar[ACCESS]["value"]})
        ).status_code
        == 200
    )

    # Re-using the rotated-out token fails; the new one works.
    again = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: refresh1}))
    assert again.status_code == 401
    ok = pg_client.post(
        f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: jar[REFRESH]["value"]})
    )
    assert ok.status_code == 200


def test_refresh_rejects_missing_unknown_and_foreign_tokens(pg_client, db_session):
    marketer = make_marketer(db_session)
    assert pg_client.post(f"{AUTH}/refresh").status_code == 401
    resp = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: "nope"}))
    assert resp.status_code == 401

    # A refresh token of another identity type (even for the same id) is not a marketer's.
    raw, token_hash = create_refresh_token()
    db_session.add(
        RefreshToken(
            user_id=marketer.id,
            user_type="tenant_user",
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            created_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    resp = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: raw}))
    assert resp.status_code == 401

    # The refresh cookie is not read from the tenant cookie name.
    _, refresh = logged_in(pg_client, marketer)
    resp = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(refresh_token=refresh))
    assert resp.status_code == 401


def test_refresh_rejects_expired_token(pg_client, db_session):
    marketer = make_marketer(db_session)
    raw, token_hash = create_refresh_token()
    db_session.add(
        RefreshToken(
            user_id=marketer.id,
            user_type="marketer",
            token_hash=token_hash,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            created_at=datetime.now(UTC) - timedelta(days=31),
        )
    )
    db_session.flush()
    resp = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: raw}))
    assert resp.status_code == 401


def test_logout_revokes_refresh(pg_client, db_session, probe_route):
    marketer = make_marketer(db_session)
    access, refresh = logged_in(pg_client, marketer)

    resp = pg_client.post(
        f"{AUTH}/logout", headers=cookie_header(**{ACCESS: access, REFRESH: refresh})
    )
    assert resp.status_code == 200
    assert resp.json() == {"message": "Logged out"}

    # Cookies are cleared (expired) on the same names/paths they were set on.
    jar = cookies_from(resp)
    assert set(jar) == {ACCESS, REFRESH}
    assert jar[ACCESS]["path"] == "/" and jar[REFRESH]["path"] == "/api/v1/auth/marketer"
    assert all(c["max-age"] == "0" for c in jar.values())

    # Server-side: refresh record revoked, access jti revoked.
    record = db_session.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hashlib.sha256(refresh.encode()).hexdigest()
        )
    ).scalar_one()
    assert record.revoked_at is not None
    assert (
        pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: refresh})).status_code
        == 401
    )
    assert pg_client.get(probe_route, headers=cookie_header(**{ACCESS: access})).status_code == 401


def test_logout_is_idempotent_and_needs_no_session(pg_client):
    for _ in range(2):
        resp = pg_client.post(f"{AUTH}/logout")
        assert resp.status_code == 200
        assert set(cookies_from(resp)) == {ACCESS, REFRESH}
    junk = pg_client.post(
        f"{AUTH}/logout", headers=cookie_header(**{ACCESS: "junk", REFRESH: "junk"})
    )
    assert junk.status_code == 200


def test_logout_does_not_revoke_another_identitys_refresh_token(pg_client, db_session):
    marketer = make_marketer(db_session)
    raw, token_hash = create_refresh_token()
    record = RefreshToken(
        user_id=marketer.id,
        user_type="tenant_user",
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        created_at=datetime.now(UTC),
    )
    db_session.add(record)
    db_session.flush()
    pg_client.post(f"{AUTH}/logout", headers=cookie_header(**{REFRESH: raw}))
    db_session.refresh(record)
    assert record.revoked_at is None


# --- set-password / forgot-password ----------------------------------------------------------


def _invite(marketer) -> str:
    return token_service_module.token_service.create_marketer_invite_token(
        marketer.id, marketer.email
    )


def test_set_password_roundtrip_from_invite_then_login(pg_client, db_session):
    marketer = make_marketer(db_session, password=None)
    assert login(pg_client, marketer, password=PASSWORD).status_code == 401

    token = _invite(marketer)
    resp = pg_client.post(f"{AUTH}/set-password", json={"token": token, "new_password": PASSWORD})
    assert resp.status_code == 200, resp.text
    assert cookies_from(resp) == {}  # setting a password does not sign anyone in
    assert PASSWORD not in resp.text and token not in resp.text

    db_session.refresh(marketer)
    assert marketer.password_hash and marketer.password_hash != PASSWORD
    assert login(pg_client, marketer).status_code == 200


def test_set_password_token_is_one_time_and_garbage_is_400(pg_client, db_session):
    marketer = make_marketer(db_session, password=None)
    token = _invite(marketer)
    body = {"token": token, "new_password": PASSWORD}
    assert pg_client.post(f"{AUTH}/set-password", json=body).status_code == 200

    reused = pg_client.post(f"{AUTH}/set-password", json={**body, "new_password": NEW_PASSWORD})
    assert reused.status_code == 400
    assert reused.json() == INVALID_TOKEN
    assert login(pg_client, marketer, password=NEW_PASSWORD).status_code == 401

    garbage = pg_client.post(
        f"{AUTH}/set-password", json={"token": "garbage-token", "new_password": PASSWORD}
    )
    assert garbage.status_code == 400
    assert garbage.json() == INVALID_TOKEN


def test_set_password_applies_the_platform_password_rules(pg_client, db_session):
    marketer = make_marketer(db_session, password=None)
    token = _invite(marketer)
    short = pg_client.post(f"{AUTH}/set-password", json={"token": token, "new_password": "short"})
    assert short.status_code == 422
    too_long = pg_client.post(
        f"{AUTH}/set-password", json={"token": token, "new_password": "x" * 101}
    )
    assert too_long.status_code == 422
    # A rejected password does not burn the token (validation runs before it is consumed).
    ok = pg_client.post(f"{AUTH}/set-password", json={"token": token, "new_password": PASSWORD})
    assert ok.status_code == 200


def test_set_password_rejects_suspended_missing_and_changed_email(pg_client, db_session):
    suspended = make_marketer(db_session, password=None, status=MarketerStatus.SUSPENDED)
    resp = pg_client.post(
        f"{AUTH}/set-password", json={"token": _invite(suspended), "new_password": PASSWORD}
    )
    assert (resp.status_code, resp.json()) == (400, INVALID_TOKEN)
    db_session.refresh(suspended)
    assert suspended.password_hash is None

    ghost = token_service_module.token_service.create_marketer_invite_token(
        uuid4(), "ghost@example.com"
    )
    resp = pg_client.post(f"{AUTH}/set-password", json={"token": ghost, "new_password": PASSWORD})
    assert (resp.status_code, resp.json()) == (400, INVALID_TOKEN)

    moved = make_marketer(db_session, password=None, email="old.address@example.com")
    stale = _invite(moved)
    moved.email = "new.address@example.com"
    db_session.flush()
    resp = pg_client.post(f"{AUTH}/set-password", json={"token": stale, "new_password": PASSWORD})
    assert (resp.status_code, resp.json()) == (400, INVALID_TOKEN)
    db_session.refresh(moved)
    assert moved.password_hash is None


def test_forgot_password_body_identical_for_known_and_unknown(pg_client, db_session, fake_email):
    active = make_marketer(db_session)
    suspended = make_marketer(db_session, status=MarketerStatus.SUSPENDED)
    responses = [
        pg_client.post(f"{AUTH}/forgot-password", json={"email": e})
        for e in (active.email, suspended.email, f"nobody_{uuid4().hex[:8]}@example.com")
    ]
    assert {r.status_code for r in responses} == {200}
    assert len({r.text for r in responses}) == 1
    assert len({r.headers["content-length"] for r in responses}) == 1
    # Only the active marketer got an email.
    assert [m["marketer_email"] for m in fake_email.sent] == [active.email]
    assert fake_email.sent[0]["reset_token"]


def test_forgot_password_survives_email_failure(pg_client, db_session, monkeypatch):
    monkeypatch.setattr(auth_service_module, "email_service", FakeEmail(raises=True))
    marketer = make_marketer(db_session)
    resp = pg_client.post(f"{AUTH}/forgot-password", json={"email": marketer.email})
    assert resp.status_code == 200


def test_reset_flow_changes_password_and_revokes_refresh_tokens(
    pg_client, db_session, fake_email, probe_route
):
    marketer = make_marketer(db_session)
    access_old, refresh_old = logged_in(pg_client, marketer)

    pg_client.post(f"{AUTH}/forgot-password", json={"email": marketer.email})
    token = fake_email.sent[0]["reset_token"]
    resp = pg_client.post(
        f"{AUTH}/set-password", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 200, resp.text

    assert login(pg_client, marketer, password=PASSWORD).status_code == 401
    assert login(pg_client, marketer, password=NEW_PASSWORD).status_code == 200
    # Sessions from before the reset cannot be refreshed.
    stale = pg_client.post(f"{AUTH}/refresh", headers=cookie_header(**{REFRESH: refresh_old}))
    assert stale.status_code == 401
    record = db_session.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == hashlib.sha256(refresh_old.encode()).hexdigest()
        )
    ).scalar_one()
    assert record.revoked_at is not None
    # Documented trade-off (same as the tenant reset flow): a password change revokes refresh
    # tokens but an already-issued access token keeps working until it expires (short-lived).
    still_ok = pg_client.get(probe_route, headers=cookie_header(**{ACCESS: access_old}))
    assert still_ok.status_code == 200


def _redis_ttl(token: str) -> int:
    return token_service_module.token_service.redis_client.ttl(f"marketer_invite:{token}")


def test_set_password_kills_every_other_outstanding_token(pg_client, db_session):
    marketer = make_marketer(db_session, password=None)
    other = make_marketer(db_session, password=None)
    svc = token_service_module.token_service
    invite = _invite(marketer)
    second_invite = _invite(marketer)
    reset = svc.create_marketer_invite_token(marketer.id, marketer.email, expiry_minutes=60)
    unrelated = _invite(other)

    ok = pg_client.post(f"{AUTH}/set-password", json={"token": invite, "new_password": PASSWORD})
    assert ok.status_code == 200, ok.text

    for stale in (second_invite, reset):
        resp = pg_client.post(
            f"{AUTH}/set-password", json={"token": stale, "new_password": NEW_PASSWORD}
        )
        assert (resp.status_code, resp.json()) == (400, INVALID_TOKEN)
    assert login(pg_client, marketer, password=NEW_PASSWORD).status_code == 401
    assert login(pg_client, marketer).status_code == 200
    # Another marketer's link is untouched.
    assert svc.redis_client.exists(f"marketer_invite:{unrelated}") == 1


def test_forgot_password_leaves_invite_and_earlier_reset_links_redeemable(
    pg_client, db_session, fake_email
):
    """The unauthenticated path only adds a token; it must never retire admin-issued links."""
    marketer = make_marketer(db_session, password=None)
    svc = token_service_module.token_service
    invite = _invite(marketer)
    earlier_reset = svc.create_marketer_invite_token(marketer.id, marketer.email, expiry_minutes=60)

    resp = pg_client.post(f"{AUTH}/forgot-password", json={"email": marketer.email})
    assert resp.status_code == 200
    new_reset = fake_email.sent[0]["reset_token"]
    assert len({invite, earlier_reset, new_reset}) == 3
    for token in (invite, earlier_reset, new_reset):
        assert svc.redis_client.exists(f"marketer_invite:{token}") == 1

    # The untouched invite still works; redeeming it then retires everything else.
    ok = pg_client.post(f"{AUTH}/set-password", json={"token": invite, "new_password": PASSWORD})
    assert ok.status_code == 200, ok.text
    for stale in (earlier_reset, new_reset):
        resp = pg_client.post(
            f"{AUTH}/set-password", json={"token": stale, "new_password": NEW_PASSWORD}
        )
        assert (resp.status_code, resp.json()) == (400, INVALID_TOKEN)


def test_forgot_password_has_a_per_marketer_email_cooldown(pg_client, db_session, fake_email):
    marketer = make_marketer(db_session)
    other = make_marketer(db_session)
    bodies = [
        pg_client.post(f"{AUTH}/forgot-password", json={"email": marketer.email}) for _ in range(3)
    ]
    assert {r.status_code for r in bodies} == {200}
    assert len({r.text for r in bodies}) == 1  # throttling is invisible to the caller
    assert len(fake_email.sent) == 1  # one email inside the cooldown window

    # Other marketers are unaffected, and the window does end.
    pg_client.post(f"{AUTH}/forgot-password", json={"email": other.email})
    assert len(fake_email.sent) == 2
    token_service_module.token_service.redis_client.delete(f"marketer_reset_cooldown:{marketer.id}")
    pg_client.post(f"{AUTH}/forgot-password", json={"email": marketer.email})
    assert len(fake_email.sent) == 3


def test_unknown_or_suspended_forgot_password_does_not_touch_existing_links(
    pg_client, db_session, fake_email
):
    marketer = make_marketer(db_session, password=None)
    invite = _invite(marketer)
    pg_client.post(f"{AUTH}/forgot-password", json={"email": "nobody@example.com"})
    assert token_service_module.token_service.redis_client.exists(f"marketer_invite:{invite}") == 1


def test_reset_token_expires_in_sixty_minutes_and_invite_keeps_seven_days(
    pg_client, db_session, fake_email
):
    marketer = make_marketer(db_session)
    pg_client.post(f"{AUTH}/forgot-password", json={"email": marketer.email})
    reset_ttl = _redis_ttl(fake_email.sent[0]["reset_token"])
    assert 3500 <= reset_ttl <= 3600
    assert fake_email.sent[0]["expiry_minutes"] == 60

    invite_ttl = _redis_ttl(_invite(marketer))
    assert 7 * 24 * 3600 - 100 <= invite_ttl <= 7 * 24 * 3600


def test_reset_email_link_points_at_marketer_set_password_page(monkeypatch):
    from app.services.email_service import EmailService

    svc = EmailService()
    captured = {}

    def fake_send(to_email, subject, html_content, *a, **k):
        captured.update(to=to_email, subject=subject, html=html_content)
        return True

    monkeypatch.setattr(svc, "_send_email", fake_send)
    sent = svc.send_marketer_password_reset_email(
        marketer_email="ada@example.com", marketer_name="Ada", reset_token="tok123"
    )
    assert sent is True
    assert f"{settings.FRONTEND_URL}/marketer/set-password?token=tok123" in captured["html"]
    assert captured["to"] == "ada@example.com"
