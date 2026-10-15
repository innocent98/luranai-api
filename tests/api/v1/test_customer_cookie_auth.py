"""Cookie-only session tests for the authenticated customer (B2C) auth surface.

The customer JWTs live in HttpOnly cookies that are distinct from the tenant
dashboard cookies (``access_token`` / ``refresh_token``) so a browser holding
both sessions never has one overwrite the other, and the login response body
no longer carries a token for JS to read.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token, get_password_hash
from app.db.models.customer_user import CustomerUser
from app.main import app
from app.utils.http_rate_limiter import auth_rate_limit

PASSWORD = "CustomerPass123!"
REFRESH_PATH = "/api/v1/customer/auth"
LEGACY_REFRESH_PATH = "/api/v1/customer/auth/refresh"


@pytest.fixture(autouse=True)
def _bypass_auth_rate_limit():
    async def _noop():
        pass

    app.dependency_overrides[auth_rate_limit] = _noop
    yield
    app.dependency_overrides.pop(auth_rate_limit, None)


@pytest.fixture()
def customer(db: Session) -> CustomerUser:
    row = CustomerUser(
        id=uuid4(),
        full_name="Cookie Customer",
        email=f"cookie-{uuid4().hex[:8]}@example.com",
        hashed_password=get_password_hash(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _login(client: TestClient, customer: CustomerUser):
    return client.post(
        "/api/v1/customer/auth/login",
        json={"email": customer.email, "password": PASSWORD},
    )


def _set_cookie_headers(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _cookie(response, name: str) -> str:
    matches = [h for h in _set_cookie_headers(response) if h.startswith(f"{name}=")]
    assert matches, f"no Set-Cookie for {name}: {_set_cookie_headers(response)}"
    return matches[0]


def _legacy_customer_token(customer: CustomerUser) -> str:
    return create_access_token(
        subject=str(customer.id),
        additional_claims={"email": customer.email, "auth_type": "customer"},
    )


def test_login_sets_dedicated_httponly_cookies_and_no_body_token(client, customer):
    response = _login(client, customer)
    assert response.status_code == 200
    body = response.json()
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert body["customer"]["email"] == customer.email

    access = _cookie(response, "customer_access_token").lower()
    refresh = _cookie(response, "customer_refresh_token").lower()
    for header in (access, refresh):
        assert "httponly" in header
        assert "secure" in header
        assert "samesite=lax" in header
    assert "path=/;" in access or access.endswith("path=/")
    assert f"path={REFRESH_PATH}".lower() in refresh
    # Never clobber the tenant dashboard cookies.
    assert not any(h.startswith("access_token=") for h in _set_cookie_headers(response))
    # Legacy shared-name refresh cookie may only ever appear as an expiry, never a value.
    for header in _set_cookie_headers(response):
        if header.startswith("refresh_token="):
            assert "max-age=0" in header.lower()


def test_me_authenticates_from_customer_cookie_alone(client, customer):
    token = (
        _cookie(_login(client, customer), "customer_access_token").split(";")[0].split("=", 1)[1]
    )
    client.cookies.clear()
    client.cookies.set("customer_access_token", token)
    response = client.get("/api/v1/customer/auth/me")
    assert response.status_code == 200
    assert response.json()["email"] == customer.email


def test_me_without_credentials_is_401(client):
    client.cookies.clear()
    assert client.get("/api/v1/customer/auth/me").status_code == 401


def test_me_still_accepts_bearer_header_for_non_browser_clients(client, customer):
    client.cookies.clear()
    response = client.get(
        "/api/v1/customer/auth/me",
        headers={"Authorization": f"Bearer {_legacy_customer_token(customer)}"},
    )
    assert response.status_code == 200


def test_me_accepts_legacy_access_token_cookie_during_migration(client, customer):
    client.cookies.clear()
    client.cookies.set("access_token", _legacy_customer_token(customer))
    assert client.get("/api/v1/customer/auth/me").status_code == 200


def test_tenant_cookie_does_not_authenticate_customer_endpoints(client, customer):
    client.cookies.clear()
    tenant_like = create_access_token(
        subject=str(uuid4()), additional_claims={"role": "tenant_admin"}
    )
    client.cookies.set("access_token", tenant_like)
    assert client.get("/api/v1/customer/auth/me").status_code == 401


def test_refresh_rotates_cookies_from_customer_refresh_cookie(client, customer):
    login = _login(client, customer)
    refresh_value = _cookie(login, "customer_refresh_token").split(";")[0].split("=", 1)[1]
    client.cookies.clear()
    client.cookies.set("customer_refresh_token", refresh_value, path=REFRESH_PATH)
    response = client.post("/api/v1/customer/auth/refresh")
    assert response.status_code == 200
    assert "access_token" not in response.json()
    new_refresh = _cookie(response, "customer_refresh_token")
    assert new_refresh.split(";")[0].split("=", 1)[1] != refresh_value
    _cookie(response, "customer_access_token")


def test_refresh_without_cookie_is_401(client):
    client.cookies.clear()
    assert client.post("/api/v1/customer/auth/refresh").status_code == 401


def test_refresh_falls_back_to_legacy_refresh_cookie(client, customer):
    login = _login(client, customer)
    legacy_value = _cookie(login, "customer_refresh_token").split(";")[0].split("=", 1)[1]
    client.cookies.clear()
    client.cookies.set("refresh_token", legacy_value, path=LEGACY_REFRESH_PATH)
    response = client.post("/api/v1/customer/auth/refresh")
    assert response.status_code == 200
    _cookie(response, "customer_refresh_token")


def test_logout_clears_cookies_revokes_token_and_blocks_reuse(client, customer):
    login = _login(client, customer)
    access = _cookie(login, "customer_access_token").split(";")[0].split("=", 1)[1]
    refresh = _cookie(login, "customer_refresh_token").split(";")[0].split("=", 1)[1]
    client.cookies.clear()
    client.cookies.set("customer_access_token", access)
    client.cookies.set("customer_refresh_token", refresh, path=REFRESH_PATH)

    response = client.post("/api/v1/customer/auth/logout")
    assert response.status_code == 200
    cleared = [h.lower() for h in _set_cookie_headers(response)]
    assert any(h.startswith("customer_access_token=") and "max-age=0" in h for h in cleared)
    assert any(h.startswith("customer_refresh_token=") and "max-age=0" in h for h in cleared)

    # The refresh token is revoked server-side, not merely forgotten.
    client.cookies.clear()
    client.cookies.set("customer_refresh_token", refresh, path=REFRESH_PATH)
    assert client.post("/api/v1/customer/auth/refresh").status_code == 401


def test_logout_does_not_clear_tenant_cookies(client, customer):
    response = client.post("/api/v1/customer/auth/logout")
    cleared = _set_cookie_headers(response)
    assert not any(h.startswith("access_token=") for h in cleared)
    # Only the customer-scoped legacy refresh path is ever expired, never the tenant's.
    assert not any(h.startswith("refresh_token=") and "path=/;" in h.lower() for h in cleared)


def _cookie_value(response, name: str) -> str:
    return _cookie(response, name).split(";")[0].split("=", 1)[1]


def test_replayed_rotated_refresh_token_is_rejected_immediately(client, customer):
    login = _login(client, customer)
    r1 = _cookie_value(login, "customer_refresh_token")
    client.cookies.clear()
    client.cookies.set("customer_refresh_token", r1, path=REFRESH_PATH)
    assert client.post("/api/v1/customer/auth/refresh").status_code == 200
    replay = client.post("/api/v1/customer/auth/refresh")
    assert replay.status_code == 401
    assert _set_cookie_headers(replay) == []


def test_logged_out_token_is_rejected(client, customer):
    login = _login(client, customer)
    r1 = _cookie_value(login, "customer_refresh_token")
    a1 = _cookie_value(login, "customer_access_token")
    client.cookies.clear()
    client.cookies.set("customer_access_token", a1)
    client.cookies.set("customer_refresh_token", r1, path=REFRESH_PATH)
    assert client.post("/api/v1/customer/auth/logout").status_code == 200
    client.cookies.clear()
    client.cookies.set("customer_refresh_token", r1, path=REFRESH_PATH)
    assert client.post("/api/v1/customer/auth/refresh").status_code == 401


def test_password_change_right_after_rotation_kills_the_old_token(client, customer):
    login = _login(client, customer)
    r1 = _cookie_value(login, "customer_refresh_token")
    client.cookies.clear()
    client.cookies.set("customer_refresh_token", r1, path=REFRESH_PATH)
    rotated = client.post("/api/v1/customer/auth/refresh")
    assert rotated.status_code == 200
    r2 = _cookie_value(rotated, "customer_refresh_token")
    a2 = _cookie_value(rotated, "customer_access_token")
    resp = client.put(
        "/api/v1/customer/auth/me/password",
        headers={"Authorization": f"Bearer {a2}"},
        json={"current_password": PASSWORD, "new_password": "NewCustomerPass456!"},
    )
    assert resp.status_code == 200
    for raw in (r1, r2):
        client.cookies.clear()
        client.cookies.set("customer_refresh_token", raw, path=REFRESH_PATH)
        assert client.post("/api/v1/customer/auth/refresh").status_code == 401


def test_logout_does_not_revoke_a_non_customer_token(client, customer, monkeypatch):
    import app.api.deps as deps

    revoked: list[str] = []
    monkeypatch.setattr(deps, "revoke_token", lambda jti, ttl: revoked.append(jti))
    tenant_token = create_access_token(
        subject=str(uuid4()), additional_claims={"role": "tenant_admin"}
    )
    client.cookies.clear()
    client.cookies.set("access_token", tenant_token)  # legacy fallback would pick this up
    assert client.post("/api/v1/customer/auth/logout").status_code == 200
    assert revoked == []


@pytest.mark.parametrize("how", ["update", "reset"])
def test_password_change_revokes_refresh_tokens(client, customer, monkeypatch, how):
    from app.services.token_service import token_service

    login = _login(client, customer)
    r1 = _cookie_value(login, "customer_refresh_token")
    access = _cookie_value(login, "customer_access_token")
    if how == "update":
        resp = client.put(
            "/api/v1/customer/auth/me/password",
            headers={"Authorization": f"Bearer {access}"},
            json={"current_password": PASSWORD, "new_password": "NewCustomerPass456!"},
        )
    else:
        monkeypatch.setattr(
            token_service,
            "verify_customer_password_reset_token",
            lambda t: {"customer_user_id": customer.id, "email": customer.email},
        )
        resp = client.post(
            "/api/v1/customer/auth/reset-password",
            json={
                "token": "x",
                "new_password": "NewCustomerPass456!",
            },
        )
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    client.cookies.set("customer_refresh_token", r1, path=REFRESH_PATH)
    assert client.post("/api/v1/customer/auth/refresh").status_code == 401
