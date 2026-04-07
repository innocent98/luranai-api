"""Integration tests for customer follows, public discovery, and tenant channel publishing."""

from datetime import UTC, datetime
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models.customer_engagement import TenantChannelPost
from app.core.security import create_access_token
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole


def _create_tenant(db: Session) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name="channel_test_tenant",
        display_name="Channel Test Tenant",
        public_slug="channel-test-tenant",
        is_publicly_listed=True,
        api_key_hash="test_hash",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _create_public_tenant(
    db: Session,
    *,
    name: str,
    display_name: str,
    slug: str,
    is_featured: bool = False,
) -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name=name,
        display_name=display_name,
        public_slug=slug,
        is_publicly_listed=True,
        is_featured=is_featured,
        api_key_hash=f"hash-{slug}",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _create_user(db: Session, tenant_id, role: UserRole, email: str) -> User:
    user = User(
        id=uuid4(),
        tenant_id=tenant_id,
        email=email,
        full_name=email.split("@")[0].title(),
        role=role,
        hashed_password="hashed",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_customer(db: Session) -> CustomerUser:
    customer = CustomerUser(
        id=uuid4(),
        full_name="Customer One",
        email="customer@example.com",
        hashed_password="hashed",
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def _tenant_headers(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        additional_claims={
            "email": user.email,
            "role": user.role.value,
            "tenant_id": str(user.tenant_id),
        },
    )
    return {"Authorization": f"Bearer {token}"}


def _customer_headers(customer: CustomerUser) -> dict[str, str]:
    token = create_access_token(
        subject=str(customer.id),
        additional_claims={
            "email": customer.email,
            "auth_type": "customer",
        },
    )
    return {"Authorization": f"Bearer {token}"}


def _publish_post(
    client: TestClient,
    headers: dict[str, str],
    *,
    title: str,
    excerpt: str,
    content: str,
    is_pinned: bool = False,
) -> str:
    create_post = client.post(
        "/api/v1/tenant/channels",
        headers=headers,
        json={
            "title": title,
            "excerpt": excerpt,
            "content": content,
            "is_pinned": is_pinned,
            "publish_now": False,
        },
    )
    assert create_post.status_code == 201
    post_id = create_post.json()["id"]

    publish_post = client.post(
        f"/api/v1/tenant/channels/{post_id}/publish",
        headers=headers,
    )
    assert publish_post.status_code == 200
    return post_id


def test_customer_follow_and_channel_feed_flow(client: TestClient, db: Session):
    tenant = _create_tenant(db)
    admin = _create_user(db, tenant.id, UserRole.TENANT_ADMIN, "admin@example.com")
    customer = _create_customer(db)

    customer_headers = _customer_headers(customer)
    tenant_headers = _tenant_headers(admin)

    follow_status = client.get(
        f"/api/v1/customer/follows/{tenant.id}/status",
        headers=customer_headers,
    )
    assert follow_status.status_code == 200
    assert follow_status.json()["is_following"] is False

    follow_response = client.post(
        f"/api/v1/customer/follows/{tenant.id}",
        headers=customer_headers,
    )
    assert follow_response.status_code == 201
    assert follow_response.json()["is_following"] is True
    assert follow_response.json()["follower_count"] == 1

    create_post = client.post(
        "/api/v1/tenant/channels",
        headers=tenant_headers,
        json={
            "title": "April Release",
            "excerpt": "A short summary for the customer feed.",
            "content": "<p>We shipped a new billing dashboard and faster support routing.</p>",
            "is_pinned": True,
            "publish_now": False,
        },
    )
    assert create_post.status_code == 201
    post_id = create_post.json()["id"]
    assert create_post.json()["status"] == "draft"

    publish_post = client.post(
        f"/api/v1/tenant/channels/{post_id}/publish",
        headers=tenant_headers,
    )
    assert publish_post.status_code == 200
    assert publish_post.json()["status"] == "published"
    assert publish_post.json()["published_at"] is not None

    follows_list = client.get("/api/v1/customer/follows", headers=customer_headers)
    assert follows_list.status_code == 200
    assert follows_list.json()[0]["latest_post_title"] == "April Release"

    channel_feed = client.get("/api/v1/customer/channels", headers=customer_headers)
    assert channel_feed.status_code == 200
    data = channel_feed.json()
    assert data["has_more"] is False
    assert len(data["posts"]) == 1
    assert data["posts"][0]["title"] == "April Release"
    assert data["posts"][0]["is_following"] is True

    channel_detail = client.get(
        f"/api/v1/customer/channels/{post_id}",
        headers=customer_headers,
    )
    assert channel_detail.status_code == 200
    assert "billing dashboard" in channel_detail.json()["content"]


def test_viewer_cannot_manage_tenant_channel_posts(client: TestClient, db: Session):
    tenant = _create_tenant(db)
    viewer = _create_user(db, tenant.id, UserRole.VIEWER, "viewer@example.com")

    response = client.post(
        "/api/v1/tenant/channels",
        headers=_tenant_headers(viewer),
        json={
            "title": "Blocked",
            "excerpt": "A short summary for the customer feed.",
            "content": "<p>This should not be allowed.</p>",
            "is_pinned": False,
            "publish_now": False,
        },
    )

    assert response.status_code == 403
    assert "permission" in response.json()["detail"].lower()


def test_customer_channel_feed_supports_pagination(client: TestClient, db: Session):
    tenant = _create_tenant(db)
    admin = _create_user(db, tenant.id, UserRole.TENANT_ADMIN, "admin2@example.com")
    customer = _create_customer(db)

    customer_headers = _customer_headers(customer)
    tenant_headers = _tenant_headers(admin)

    client.post(f"/api/v1/customer/follows/{tenant.id}", headers=customer_headers)

    first_post_id = _publish_post(
        client,
        tenant_headers,
        title="First update",
        excerpt="First update excerpt.",
        content="<p>First update content for customers.</p>",
    )
    second_post_id = _publish_post(
        client,
        tenant_headers,
        title="Second update",
        excerpt="Second update excerpt.",
        content="<p>Second update content for customers.</p>",
    )

    first_page = client.get(
        "/api/v1/customer/channels?limit=1",
        headers=customer_headers,
    )
    assert first_page.status_code == 200
    first_data = first_page.json()
    assert first_data["has_more"] is True
    assert len(first_data["posts"]) == 1
    assert first_data["posts"][0]["id"] == second_post_id

    cursor = first_data["posts"][0]["published_at"]
    second_page = client.get(
        f"/api/v1/customer/channels?limit=1&before={cursor}",
        headers=customer_headers,
    )
    assert second_page.status_code == 200
    second_data = second_page.json()
    assert len(second_data["posts"]) == 1
    assert second_data["posts"][0]["id"] == first_post_id
    assert second_data["has_more"] is False


def test_customer_channel_feed_rejects_invalid_before_cursor(client: TestClient, db: Session):
    customer = _create_customer(db)
    response = client.get(
        "/api/v1/customer/channels?before=not-a-timestamp",
        headers=_customer_headers(customer),
    )
    assert response.status_code == 422


def test_public_business_directory_prioritizes_featured(client: TestClient, db: Session):
    _create_public_tenant(
        db,
        name="beta_support",
        display_name="Beta Support",
        slug="beta-support",
        is_featured=False,
    )
    _create_public_tenant(
        db,
        name="alpha_support",
        display_name="Alpha Support",
        slug="alpha-support",
        is_featured=True,
    )

    response = client.get("/api/v1/public/businesses?page_size=10")
    assert response.status_code == 200
    businesses = response.json()["businesses"]
    assert businesses[0]["display_name"] == "Alpha Support"
    assert businesses[0]["is_featured"] is True
    assert businesses[1]["display_name"] == "Beta Support"


def test_tenant_admin_can_manage_channel_post_lifecycle(client: TestClient, db: Session):
    tenant = _create_tenant(db)
    admin = _create_user(db, tenant.id, UserRole.TENANT_ADMIN, "admin3@example.com")
    headers = _tenant_headers(admin)

    create_response = client.post(
        "/api/v1/tenant/channels",
        headers=headers,
        json={
            "title": "Lifecycle update",
            "excerpt": "Lifecycle update excerpt.",
            "content": "<p>Lifecycle update content for customers.</p>",
            "is_pinned": False,
            "publish_now": False,
        },
    )
    assert create_response.status_code == 201
    post_id = create_response.json()["id"]
    assert create_response.json()["status"] == "draft"

    update_response = client.put(
        f"/api/v1/tenant/channels/{post_id}",
        headers=headers,
        json={"title": "Updated lifecycle post", "is_pinned": True},
    )
    assert update_response.status_code == 200
    assert update_response.json()["title"] == "Updated lifecycle post"
    assert update_response.json()["is_pinned"] is True

    publish_response = client.post(f"/api/v1/tenant/channels/{post_id}/publish", headers=headers)
    assert publish_response.status_code == 200
    assert publish_response.json()["status"] == "published"

    overview_response = client.get("/api/v1/tenant/channels/overview", headers=headers)
    assert overview_response.status_code == 200
    overview = overview_response.json()
    assert overview["published"] == 1
    assert overview["drafts"] == 0

    list_response = client.get("/api/v1/tenant/channels", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == post_id

    archive_response = client.post(f"/api/v1/tenant/channels/{post_id}/archive", headers=headers)
    assert archive_response.status_code == 200
    assert archive_response.json()["status"] == "archived"

    refreshed = db.get(TenantChannelPost, UUID(post_id))
    assert refreshed is not None
    assert refreshed.status.value == "archived"
