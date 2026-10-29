"""Drive a RUNNING backend + worker + fake sidecar and record the T15 exclusion exchanges.

Same rig as `live_capture.py` (migrated scratch Postgres, Redis, `live_stub api`, `live_stub worker`,
`fake_sidecar`); produces docs/fe-integration-captures/whatsapp-exclusion/. NOT part of the test
suite. Numbers are obviously fake (234800000xxxx); the bearer token is replaced by a placeholder.
"""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("CAPTURE_OUT", "docs/fe-integration-captures/whatsapp-exclusion")

from tests.fakes.live_capture import (  # noqa: E402
    API,
    FAKE,
    OUT,
    client,
    db_value,
    emit,
    login,
    record,
    seed,
    sent,
    wait_for,
)

MUM = "2348000000100"  # the excluded personal contact (placeholder)
MUM_JID = f"{MUM}@s.whatsapp.net"
MUM_LID = "5550000100@lid"
CUSTOMER_JID = "2348000000200@s.whatsapp.net"  # a real customer (placeholder)
OWN = "2348000000001"
SECOND_OWN = "2348000000002"


def counts() -> dict:
    return {
        "whatsapp_chats": db_value("SELECT count(*) FROM whatsapp_chats"),
        "conversation_sessions": db_value("SELECT count(*) FROM conversation_sessions"),
        "messages": db_value("SELECT count(*) FROM messages"),
        "visitors": db_value("SELECT count(*) FROM visitors"),
        "whatsapp_inbound_messages": db_value("SELECT count(*) FROM whatsapp_inbound_messages"),
        "sent_to_sidecar": len(sent()),
    }


def main() -> None:
    tenants = seed()
    platform = login(
        os.environ["FIRST_SUPERUSER_EMAIL"],
        os.environ["FIRST_SUPERUSER_PASSWORD"],
        path="/api/v1/platform/auth/login",
    )
    alpha = login("owner-alpha@example.com", "Capture-Pass-123!")
    beta = login("owner-beta@example.com", "Capture-Pass-123!")

    consent = client.get(
        API + "/api/v1/whatsapp/consent", headers={"Authorization": f"Bearer {alpha}"}
    ).json()

    def connect(token: str, phone: str) -> tuple[str, str]:
        made = client.post(
            API + "/api/v1/whatsapp/connections",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "phone_number": phone,
                "label": "Main line",
                "consent": {"version": consent["version"], "accepted": True},
            },
        )
        made.raise_for_status()
        cid = made.json()["id"]
        key = db_value("SELECT session_key FROM whatsapp_connections WHERE id = :i", i=cid)
        client.post(f"{FAKE}/_fake/status/{key}", json={"status": "connecting"})
        client.post(f"{FAKE}/_fake/status/{key}", json={"status": "connected"})
        wait_for(
            lambda: client.get(
                f"{API}/api/v1/whatsapp/connections/{cid}",
                headers={"Authorization": f"Bearer {token}"},
            )
            .json()
            .get("status")
            == "connected",
            "connected",
        )
        return cid, key

    cid, key = connect(alpha, OWN)
    bcid, _ = connect(beta, SECOND_OWN)
    client.put(
        API + "/api/v1/tenant/ai-config",
        headers={"Authorization": f"Bearer {alpha}"},
        json={"first_responder": "ai", "grounding_requirement": False},
    ).raise_for_status()

    base = f"/api/v1/whatsapp/connections/{cid}/exclusions"

    # --- empty list, validation, add ---
    record("list-empty", "GET", base, token=alpha)
    record(
        "add-invalid-phone-national-format",
        "POST",
        base,
        token=alpha,
        json_body={"phone_number": "08000000100"},
    )
    record(
        "add-invalid-phone-garbage", "POST", base, token=alpha, json_body={"phone_number": "mum"}
    )
    record("add-own-number", "POST", base, token=alpha, json_body={"phone_number": f"+{OWN}"})
    added = record(
        "add-201",
        "POST",
        base,
        token=alpha,
        json_body={"phone_number": "+234 800 000 0100", "label": "Mum"},
        note="Typed with +, spaces: normalised to digits server-side. The full number is not echoed.",
    ).json()
    record("add-duplicate-409", "POST", base, token=alpha, json_body={"phone_number": MUM})
    record(
        "add-no-label-201", "POST", base, token=alpha, json_body={"phone_number": "2348000000101"}
    )
    record("list-two", "GET", base, token=alpha)
    record("list-page", "GET", base + "?limit=1&offset=1", token=alpha)
    record("list-bad-limit-422", "GET", base + "?limit=0", token=alpha)

    # --- bulk ---
    record(
        "bulk-partial",
        "POST",
        base + "/bulk",
        token=alpha,
        json_body={
            "entries": [
                {"phone_number": "2348000000102", "label": "Dad"},
                {"phone_number": MUM},
                {"phone_number": "2348000000102"},
                {"phone_number": "08000000103"},
                {"phone_number": f"+{OWN}"},
                {"phone_number": "+234 800 000 0104"},
            ]
        },
    )
    record("bulk-empty-422", "POST", base + "/bulk", token=alpha, json_body={"entries": []})
    record("list-after-bulk", "GET", base, token=alpha)

    # --- isolation + gating ---
    record("cross-tenant-list-404", "GET", base, token=beta)
    record(
        "cross-tenant-add-404",
        "POST",
        base,
        token=beta,
        json_body={"phone_number": "2348000000300"},
    )
    record("cross-tenant-delete-404", "DELETE", f"{base}/{added['id']}", token=beta)
    record(
        "unknown-connection-404",
        "GET",
        "/api/v1/whatsapp/connections/00000000-0000-0000-0000-000000000000/exclusions",
        token=alpha,
    )
    record(
        "platform-revoke-beta",
        "PUT",
        f"/api/v1/platform/tenants/{tenants['beta']}/capabilities",
        token=platform,
        json_body={"can_use_whatsapp": False},
    )
    bbase = f"/api/v1/whatsapp/connections/{bcid}/exclusions"
    record(
        "add-not-enabled-403",
        "POST",
        bbase,
        token=beta,
        json_body={"phone_number": "2348000000300"},
    )
    record(
        "list-allowed-when-not-enabled",
        "GET",
        bbase,
        token=beta,
        note="List and remove are not plan-gated.",
    )

    # --- the pipeline: an excluded contact leaves no trace ---
    evidence: dict = {"before": counts()}
    steps = []

    def send(name: str, body: dict) -> None:
        out = emit(key, body)
        time.sleep(1.2)
        steps.append(
            {"step": name, "event": body, "webhook_response": out, "counts_after": counts()}
        )

    send(
        "inbound text, phone-JID ref",
        {
            "type": "message.inbound",
            "message_id": "X1",
            "from_ref": MUM_JID,
            "from_phone_digits": MUM,
            "push_name": "Mum",
            "kind": "text",
            "text": "Are you coming for dinner?",
        },
    )
    send(
        "inbound text, LID ref with resolved digits (learns the LID)",
        {
            "type": "message.inbound",
            "message_id": "X2",
            "from_ref": MUM_LID,
            "from_phone_digits": MUM,
            "push_name": "Mum",
            "kind": "text",
            "text": "Hello?",
        },
    )
    send(
        "inbound text, LID ref and NO digits (matches the learned LID)",
        {
            "type": "message.inbound",
            "message_id": "X3",
            "from_ref": MUM_LID,
            "from_phone_digits": None,
            "push_name": "Mum",
            "kind": "text",
            "text": "Call me",
        },
    )
    send(
        "inbound voice note (kind other)",
        {
            "type": "message.inbound",
            "message_id": "X4",
            "from_ref": MUM_LID,
            "from_phone_digits": None,
            "push_name": "Mum",
            "kind": "other",
            "text": "",
        },
    )
    send(
        "owner types to the excluded contact",
        {
            "type": "message.outbound_external",
            "message_id": "X5",
            "to_ref": MUM_LID,
            "text": "On my way",
        },
    )
    evidence["steps_excluded"] = steps
    evidence["after_excluded"] = counts()
    assert evidence["after_excluded"] == evidence["before"], "an excluded contact left a trace"

    steps2: list = []
    steps = steps2
    send(
        "a real customer on the same number is answered",
        {
            "type": "message.inbound",
            "message_id": "C1",
            "from_ref": CUSTOMER_JID,
            "from_phone_digits": "2348000000200",
            "push_name": "Ada",
            "kind": "text",
            "text": "What are your opening hours?",
        },
    )
    wait_for(lambda: len(sent()) >= 1, "AI reply to the real customer")
    evidence["steps_customer"] = steps2
    evidence["after_customer"] = counts()

    # removal restores normal handling
    record("remove-204", "DELETE", f"{base}/{added['id']}", token=alpha)
    steps3: list = []
    steps = steps3
    send(
        "after removal the same number is answered",
        {
            "type": "message.inbound",
            "message_id": "X6",
            "from_ref": MUM_JID,
            "from_phone_digits": MUM,
            "push_name": "Mum",
            "kind": "text",
            "text": "Hi again",
        },
    )
    wait_for(lambda: len(sent()) >= 2, "AI reply after removal")
    evidence["steps_after_removal"] = steps3
    evidence["after_removal"] = counts()
    record("remove-again-404", "DELETE", f"{base}/{added['id']}", token=alpha)
    record("list-final", "GET", base, token=alpha)

    (OUT / "pipeline-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("pipeline evidence written")


if __name__ == "__main__":
    main()
