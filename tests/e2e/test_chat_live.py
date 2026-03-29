from __future__ import annotations

import pytest

from tests.e2e.helpers import assert_ok, post_chat, unique_user


pytestmark = pytest.mark.e2e


def test_professional_chat_automation_happy_path(pro_client):
    tenant = assert_ok(pro_client.get("/tenant"))
    user_identifier = unique_user("staging-pro-order")

    first = post_chat(
        pro_client,
        tenant_id=tenant["id"],
        user_identifier=user_identifier,
        message="what is my order status?",
    )
    assert "order" in first["response"].lower()

    second = post_chat(
        pro_client,
        session_id=first["session_id"],
        user_identifier=user_identifier,
        message="ORD1234",
    )
    normalized = second["response"].lower()
    assert "processing" in normalized or "in_transit" in normalized


def test_lite_plan_does_not_offer_lookup_chain_prompt(lite_client):
    tenant = assert_ok(lite_client.get("/tenant"))
    user_identifier = unique_user("staging-lite-account")

    response = post_chat(
        lite_client,
        tenant_id=tenant["id"],
        user_identifier=user_identifier,
        message="I want to check my account status",
    )
    assert "email address" not in response["response"].lower()


def test_execution_limit_blocks_ai_chat_automation(quota_client):
    tenant = assert_ok(quota_client.get("/tenant"))
    user_identifier = unique_user("staging-quota-order")

    first = post_chat(
        quota_client,
        tenant_id=tenant["id"],
        user_identifier=user_identifier,
        message="what is my order status?",
    )
    second = post_chat(
        quota_client,
        session_id=first["session_id"],
        user_identifier=user_identifier,
        message="ORD1234",
    )
    normalized = second["response"].lower()
    assert "limit" in normalized or "upgrade" in normalized
