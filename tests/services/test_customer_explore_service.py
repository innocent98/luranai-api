"""Tests for CustomerExploreService.get_personal_businesses (T8)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.conversation import ConversationSession
from app.db.models.customer_engagement import CustomerBusinessFollow
from app.db.models.customer_user import CustomerUser
from app.db.models.tenant import Tenant
from app.services.customer_explore_service import CustomerExploreService


@pytest.fixture
def customer(db):
    c = CustomerUser(
        full_name="Test Customer",
        email=f"test+{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        chat_user_key=f"ck_{uuid.uuid4().hex[:8]}",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_tenant(db, name: str, listed: bool = True) -> Tenant:
    t = Tenant(
        name=f"{name}_{uuid.uuid4().hex[:8]}",
        display_name=name,
        api_key_hash=f"hash_{uuid.uuid4().hex}",
        public_slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        is_publicly_listed=listed,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _follow(db, customer, tenant, days_ago: int = 0):
    f = CustomerBusinessFollow(
        customer_user_id=customer.id,
        tenant_id=tenant.id,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.add(f)
    db.commit()
    return f


def _chat(db, customer, tenant, days_ago: int):
    s = ConversationSession(
        tenant_id=tenant.id,
        customer_user_id=customer.id,
        user_identifier=f"customer:{customer.id}",
        last_message_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.add(s)
    db.commit()
    return s


class TestGetPersonalBusinesses:

    def test_empty_when_no_follows_or_chats(self, db, customer):
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert result.businesses == []

    def test_includes_followed_business(self, db, customer):
        t = _make_tenant(db, "FollowedCo")
        _follow(db, customer, t, days_ago=2)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 1
        item = result.businesses[0]
        assert item.display_name == "FollowedCo"
        assert item.relationship.is_followed is True

    def test_includes_recently_chatted_business_within_30_days(self, db, customer):
        t = _make_tenant(db, "ChattedCo")
        _chat(db, customer, t, days_ago=10)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 1
        assert result.businesses[0].relationship.is_followed is False
        assert result.businesses[0].relationship.last_chat_at is not None

    def test_excludes_chats_older_than_30_days(self, db, customer):
        t = _make_tenant(db, "OldChatCo")
        _chat(db, customer, t, days_ago=45)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert result.businesses == []

    def test_dedupe_when_followed_and_recently_chatted(self, db, customer):
        t = _make_tenant(db, "BothCo")
        _follow(db, customer, t, days_ago=20)
        _chat(db, customer, t, days_ago=2)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 1
        item = result.businesses[0]
        assert item.relationship.is_followed is True
        assert item.relationship.last_chat_at is not None

    def test_excludes_unlisted_tenants(self, db, customer):
        t = _make_tenant(db, "UnlistedCo", listed=False)
        _follow(db, customer, t)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert result.businesses == []

    def test_caps_at_eight_items(self, db, customer):
        for i in range(12):
            t = _make_tenant(db, f"Tenant{i:02d}")
            _follow(db, customer, t, days_ago=i)
        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        assert len(result.businesses) == 8

    def test_sorts_by_last_activity_descending(self, db, customer):
        # 3 tenants with distinct most-recent-activity timestamps
        t_old = _make_tenant(db, "Oldest")
        _follow(db, customer, t_old, days_ago=20)

        t_mid = _make_tenant(db, "Middle")
        _follow(db, customer, t_mid, days_ago=10)

        t_new = _make_tenant(db, "Newest")
        _chat(db, customer, t_new, days_ago=1)

        svc = CustomerExploreService(db)
        result = svc.get_personal_businesses(customer_user_id=customer.id)
        names = [b.display_name for b in result.businesses]
        assert names == ["Newest", "Middle", "Oldest"]
