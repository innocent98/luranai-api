"""Stop-ship regression found in review: the auto-escalate branch skips the model section, so
the offerings variables must exist on that path too (every tenant, with or without a catalog)."""

import pytest

from tests.offerings_ai.helpers import (
    add_offering,
    automation_patch,
    make_tenant,
    retrieval_patch,
    send,
)

APOLOGY = "I apologize, but I'm having trouble"


@pytest.mark.parametrize("with_items", [False, True])
@pytest.mark.parametrize("sales", [False, True])
def test_asking_for_a_human_works_for_every_tenant(client, db, with_items, sales):
    tid = make_tenant(db, sales_enabled=sales)
    if with_items:
        add_offering(db, tid, 1, "Canvas sneaker black", price=12000)
    with retrieval_patch(), automation_patch():
        r = send(client, tid, "I want to talk to a human agent please")
    assert r.status_code == 200, r.text
    body = r.json()
    assert APOLOGY not in body["message"]["content"]
    assert "team" in body["message"]["content"].lower()
    assert "offerings" not in body["message"]["meta"]
