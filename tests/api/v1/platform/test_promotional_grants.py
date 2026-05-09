from fastapi import status


def test_preview_returns_matched_count_and_token(
    pg_client, super_admin_token, churned_tenants_factory
):
    churned_tenants_factory(count=3, status="trial_expired", churned_days_ago=30)
    response = pg_client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={
            "filters": {
                "subscription_statuses": ["trial_expired"],
                "churned_within_days": 90,
            },
            "exclude_already_granted": True,
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["matched_count"] >= 3  # may include other test tenants if rollback isn't perfect
    assert "preview_token" in body
    assert len(body["sample_tenants"]) >= 3


def test_preview_requires_super_admin(pg_client, regular_user_token):
    response = pg_client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {regular_user_token}"},
    )
    assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)


def test_bulk_create_happy_path(pg_client, super_admin_token, db_session, churned_tenants_factory):
    churned_tenants_factory(count=2, status="trial_expired", churned_days_ago=30)
    preview = pg_client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()

    response = pg_client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "WINBACK_API_TEST",
            "campaign_name": "API Test",
            "granted_tier": "professional",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "API integration test",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["granted"] == 2
    assert body["skipped_already_granted"] == 0
    assert body["campaign_code"] == "WINBACK_API_TEST"


def test_bulk_create_rejects_invalid_preview_token(pg_client, super_admin_token):
    response = pg_client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": "obviously-fake-token",
            "campaign_code": "WINBACK_API_TEST",
            "campaign_name": "API Test",
            "granted_tier": "professional",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "should fail",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 400


def test_bulk_create_rejects_enterprise_tier(pg_client, super_admin_token, churned_tenants_factory):
    churned_tenants_factory(count=1, status="trial_expired", churned_days_ago=30)
    preview = pg_client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()
    response = pg_client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "WINBACK_API_TEST",
            "campaign_name": None,
            "granted_tier": "enterprise",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "should fail validation",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 422  # Pydantic Literal violation
