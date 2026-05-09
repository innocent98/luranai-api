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


def test_revoke_endpoint_reverts_tenant(
    pg_client, super_admin_token, churned_tenants_factory
):
    churned_tenants_factory(count=1, status="trial_expired", churned_days_ago=30)
    preview = pg_client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()

    bulk = pg_client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "REVOKE_TEST_CAMP",
            "campaign_name": "Revoke Test Campaign",
            "granted_tier": "professional",
            "duration_days": 30,
            "force_replace_active": False,
            "reason": "Revoke endpoint integration test",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()

    assert bulk["granted"] >= 1
    grant_id = bulk["grant_ids_sample"][0]

    response = pg_client.post(
        f"/api/v1/platform/promotional-grants/{grant_id}/revoke",
        json={"reason": "Integration test revocation reason"},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "revoked"
    assert body["grant_id"] == grant_id


def test_list_grants_for_tenant(
    pg_client, super_admin_token, churned_tenants_factory
):
    tenants = churned_tenants_factory(count=1, status="trial_expired", churned_days_ago=30)
    tenant_id = str(tenants[0].id)

    preview = pg_client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()

    pg_client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "TENANT_LIST_TEST",
            "campaign_name": "Tenant List Test",
            "granted_tier": "growth",
            "duration_days": 14,
            "force_replace_active": False,
            "reason": "Tenant grants list integration test",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )

    response = pg_client.get(
        f"/api/v1/platform/promotional-grants/tenant/{tenant_id}",
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert isinstance(body, list)
    assert len(body) >= 1


def test_list_campaigns_aggregates_stats(pg_client, super_admin_token, churned_tenants_factory):
    churned_tenants_factory(count=2, status="trial_expired", churned_days_ago=30)
    preview = pg_client.post(
        "/api/v1/platform/promotional-grants/preview",
        json={"filters": {"subscription_statuses": ["trial_expired"], "churned_within_days": 90}},
        headers={"Authorization": f"Bearer {super_admin_token}"},
    ).json()
    pg_client.post(
        "/api/v1/platform/promotional-grants/",
        json={
            "preview_token": preview["preview_token"],
            "campaign_code": "WINBACK_STATS",
            "campaign_name": "Stats Test",
            "granted_tier": "professional",
            "duration_days": 90,
            "force_replace_active": False,
            "reason": "setup for stats test",
        },
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    response = pg_client.get(
        "/api/v1/platform/promotional-grants/campaigns",
        headers={"Authorization": f"Bearer {super_admin_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "campaigns" in body
    target = next((c for c in body["campaigns"] if c["campaign_code"] == "WINBACK_STATS"), None)
    assert target is not None
    assert target["stats"]["total"] >= 2
    assert target["stats"]["active"] >= 2
    assert target["stats"]["by_source"]["admin_bulk"] >= 2
