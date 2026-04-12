"""API tests for B2C customer authentication and profile endpoints."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import create_access_token, get_password_hash
from app.db.models.customer_user import CustomerUser
from app.main import app
from app.services.email_service import email_service
from app.services.token_service import token_service
from app.utils.http_rate_limiter import auth_rate_limit


@pytest.fixture(autouse=True)
def _bypass_auth_rate_limit():
    """Disable auth rate limiting for all tests in this module."""

    async def _noop():
        pass

    app.dependency_overrides[auth_rate_limit] = _noop
    yield
    app.dependency_overrides.pop(auth_rate_limit, None)


def _create_customer(
    db: Session,
    *,
    email: str = "customer-auth@example.com",
    password: str = "CustomerPass123!",
    is_active: bool = True,
    verified: bool = True,
) -> CustomerUser:
    customer = CustomerUser(
        id=uuid4(),
        full_name="Customer Auth",
        email=email,
        hashed_password=get_password_hash(password),
        is_active=is_active,
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _customer_headers(customer: CustomerUser) -> dict[str, str]:
    token = create_access_token(
        subject=str(customer.id),
        additional_claims={
            "email": customer.email,
            "auth_type": "customer",
        },
    )
    return {"Authorization": f"Bearer {token}"}


def test_customer_register_verify_login_and_profile_flow(
    client: TestClient,
    db: Session,
    monkeypatch,
):
    sent_verification = {}
    sent_reset = {}

    monkeypatch.setattr(
        token_service,
        "create_customer_email_verification_token",
        lambda customer_user_id, email, expiry_hours=24: "verify-token",
    )
    monkeypatch.setattr(
        email_service,
        "send_verification_email",
        lambda **kwargs: sent_verification.update(kwargs) or True,
    )
    monkeypatch.setattr(
        token_service,
        "verify_customer_email_token",
        lambda token: {
            "customer_user_id": sent_verification["customer_user_id"],
            "email": sent_verification["email"],
        }
        if token == "verify-token"
        else None,
    )

    register_response = client.post(
        "/api/v1/customer/auth/register",
        json={
            "full_name": "Ada Customer",
            "email": "ada.customer@example.com",
            "password": "CustomerPass123!",
            "confirm_password": "CustomerPass123!",
            "accepted_terms": True,
        },
    )
    assert register_response.status_code == 201

    created_customer = db.query(CustomerUser).filter_by(email="ada.customer@example.com").one()
    created_customer_id = created_customer.id
    created_customer_email = created_customer.email
    sent_verification["customer_user_id"] = created_customer_id
    sent_verification["email"] = created_customer_email

    verify_response = client.post(
        "/api/v1/customer/auth/verify-email",
        json={"token": "verify-token"},
    )
    assert verify_response.status_code == 200

    resend_response = client.post(
        "/api/v1/customer/auth/resend-verification",
        json={"email": "ada.customer@example.com"},
    )
    assert resend_response.status_code == 400
    assert "already verified" in resend_response.json()["detail"].lower()

    login_response = client.post(
        "/api/v1/customer/auth/login",
        json={
            "email": "ada.customer@example.com",
            "password": "CustomerPass123!",
        },
    )
    assert login_response.status_code == 200
    auth_payload = login_response.json()
    assert auth_payload["customer"]["email"] == "ada.customer@example.com"
    assert auth_payload["token_type"] == "bearer"
    assert "access_token" in auth_payload

    headers = {"Authorization": f"Bearer {auth_payload['access_token']}"}
    me_response = client.get("/api/v1/customer/auth/me", headers=headers)
    assert me_response.status_code == 200
    assert me_response.json()["full_name"] == "Ada Customer"

    update_profile_response = client.put(
        "/api/v1/customer/auth/me",
        headers=headers,
        json={"full_name": "Ada Customer Updated"},
    )
    assert update_profile_response.status_code == 200
    assert update_profile_response.json()["full_name"] == "Ada Customer Updated"

    update_password_response = client.put(
        "/api/v1/customer/auth/me/password",
        headers=headers,
        json={
            "current_password": "CustomerPass123!",
            "new_password": "CustomerPass456!",
        },
    )
    assert update_password_response.status_code == 200

    monkeypatch.setattr(
        token_service,
        "create_customer_password_reset_token",
        lambda customer_user_id, email, expiry_minutes=15: "reset-token",
    )
    monkeypatch.setattr(
        email_service,
        "send_password_reset_email",
        lambda **kwargs: sent_reset.update(kwargs) or True,
    )

    forgot_response = client.post(
        "/api/v1/customer/auth/forgot-password",
        json={"email": "ada.customer@example.com"},
    )
    assert forgot_response.status_code == 200
    assert "reset link" in forgot_response.json()["message"].lower()

    monkeypatch.setattr(
        token_service,
        "verify_customer_password_reset_token",
        lambda token: {
            "customer_user_id": created_customer_id,
            "email": created_customer_email,
        }
        if token == "reset-token"
        else None,
    )

    reset_response = client.post(
        "/api/v1/customer/auth/reset-password",
        json={
            "token": "reset-token",
            "new_password": "ResetPass789!",
        },
    )
    assert reset_response.status_code == 200
    assert "reset successfully" in reset_response.json()["message"].lower()


def test_customer_login_rejects_unverified_user(client: TestClient, db: Session):
    _create_customer(
        db,
        email="pending.customer@example.com",
        password="CustomerPass123!",
        is_active=False,
        verified=False,
    )
    response = client.post(
        "/api/v1/customer/auth/login",
        json={
            "email": "pending.customer@example.com",
            "password": "CustomerPass123!",
        },
    )
    assert response.status_code == 401


def test_customer_profile_requires_authentication(client: TestClient):
    response = client.get("/api/v1/customer/auth/me")
    assert response.status_code == 401
