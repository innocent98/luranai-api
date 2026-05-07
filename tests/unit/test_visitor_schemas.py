"""Unit tests for visitor schemas."""

from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.visitor import PublicBusinessDirectoryItem


def test_directory_item_includes_category_and_created_at():
    item = PublicBusinessDirectoryItem(
        tenant_id=uuid4(),
        public_slug="test",
        display_name="Test Co",
        description=None,
        support_headline=None,
        support_subheadline=None,
        widget_logo_url=None,
        is_featured=False,
        category="retail_ecommerce",
        created_at=datetime.now(UTC),
    )
    assert item.category == "retail_ecommerce"


def test_directory_item_category_optional():
    item = PublicBusinessDirectoryItem(
        tenant_id=uuid4(),
        public_slug="test",
        display_name="Test Co",
        description=None,
        support_headline=None,
        support_subheadline=None,
        widget_logo_url=None,
        is_featured=False,
        category=None,
        created_at=datetime.now(UTC),
    )
    assert item.category is None
