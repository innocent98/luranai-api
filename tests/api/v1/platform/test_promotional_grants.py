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
