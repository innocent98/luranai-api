"""Spec 7.1 / plan risk 2: a tenant without active offerings gets a byte-identical prompt.

`golden_prompts.json` was captured from the code as it stood before the offerings work
(S1's head `bd1ddc6`), with a fixed tenant name. These tests fail if the model ever sees a
different byte for such a tenant: plain support mode (user prompt and system message) and sales
mode (system message, user prompt and the strict response schema).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.db.models.offering import OfferingStatus
from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
    set_settings,
)

GOLDEN = json.loads((Path(__file__).parent / "golden_prompts.json").read_text())

QUESTION = "how much is the black sneaker?"

_SALES_JSON = json.dumps(
    {
        "reply": "Hi",
        "intent": "PRICING_INQUIRY",
        "signals": {
            "asked_features": False,
            "requested_demo": False,
            "mentioned_competitor": False,
            "ready_to_buy": False,
            "offer_meeting": False,
            "offer_quote": False,
        },
        "captured": {
            "name": None,
            "email": None,
            "company": None,
            "team_size": None,
            "industry": None,
            "plan_interest": None,
        },
    }
)


def _capture_plain(client, tenant_id) -> dict:
    seen: dict = {}

    async def fake_plain(
        self, prompt, max_tokens=800, model_name=None, system_message=None, image_urls=None
    ):
        seen["prompt"] = prompt.full_prompt
        seen["system"] = system_message or self.RUNTIME_SYSTEM_MESSAGE
        return "Hello", 30, False

    with (
        patch("app.services.rag_service.RAGService.generate_ai_response", new=fake_plain),
        retrieval_patch(),
        automation_patch(),
    ):
        r = send(client, tenant_id, QUESTION)
    assert r.status_code == 200, r.text
    return seen


def _capture_sales(client, tenant_id) -> dict:
    seen: dict = {}

    async def fake_raw(self, messages, *, model, response_format=None, max_tokens=800):
        seen["messages"] = messages
        seen["format"] = response_format
        return _SALES_JSON, 40, False

    with (
        patch("app.services.rag_service.RAGService._call_openai_raw", new=fake_raw),
        retrieval_patch(),
        automation_patch(),
    ):
        r = send(client, tenant_id, QUESTION)
    assert r.status_code == 200, r.text
    return seen


def _assert_plain_identical(seen: dict) -> None:
    assert seen["prompt"] == GOLDEN["plain_prompt"]
    assert seen["system"] == GOLDEN["plain_system"]


def _assert_sales_identical(seen: dict) -> None:
    assert seen["messages"] == GOLDEN["sales_messages"]
    assert seen["format"] == GOLDEN["sales_format"]


def test_support_tenant_without_items_is_byte_identical(client, db):
    _assert_plain_identical(_capture_plain(client, make_tenant(db)))


def test_sales_tenant_without_items_is_byte_identical(client, db):
    _assert_sales_identical(_capture_sales(client, make_tenant(db, sales_enabled=True)))


@pytest.mark.parametrize("sales", [False, True])
def test_only_archived_items_is_byte_identical(client, db, sales):
    tenant_id = make_tenant(db, sales_enabled=sales)
    add_offering(db, tenant_id, 1, "Black sneaker", status=OfferingStatus.ARCHIVED)
    if sales:
        _assert_sales_identical(_capture_sales(client, tenant_id))
    else:
        _assert_plain_identical(_capture_plain(client, tenant_id))


@pytest.mark.parametrize("sales", [False, True])
def test_disabled_catalog_is_byte_identical(client, db, sales):
    tenant_id = make_tenant(db, sales_enabled=sales)
    add_offering(db, tenant_id, 1, "Black sneaker")
    set_settings(db, tenant_id, enabled=False, next_seq=2)
    if sales:
        _assert_sales_identical(_capture_sales(client, tenant_id))
    else:
        _assert_plain_identical(_capture_plain(client, tenant_id))


def test_other_tenants_items_do_not_leak(client, db):
    other = make_tenant(db)
    add_offering(db, other, 1, "Black sneaker")
    _assert_plain_identical(_capture_plain(client, make_tenant(db)))
