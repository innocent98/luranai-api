"""Repeatable smoke/regression check for tenant automation and flow integration.

This script exercises the live API against a known tenant and validates:
- tenant automation inventory and AI-chat eligibility
- direct AI-chat action success/failure
- AI lookup chaining
- approval-required action escalation
- flow action_call execution and saved output mapping

It is intended to be safe to run repeatedly in local/dev environments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class SmokeConfig:
    base_url: str
    tenant_id: str
    email: str
    password: str
    order_flow_name: str
    timeout_seconds: int
    suffix: str


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def parse_args() -> SmokeConfig:
    parser = argparse.ArgumentParser(
        description="Run live smoke checks for automation actions and flow action nodes."
    )
    parser.add_argument(
        "--base-url",
        default=_env("LURAN_SMOKE_BASE_URL", "http://localhost:8000/api/v1"),
        help="Base API URL, for example http://localhost:8000/api/v1",
    )
    parser.add_argument(
        "--tenant-id",
        default=_env("LURAN_SMOKE_TENANT_ID", "9ce5d6c5-f67e-4a73-86ab-0f9ce85d701e"),
        help="Tenant UUID to test",
    )
    parser.add_argument(
        "--email",
        default=_env("LURAN_SMOKE_EMAIL", "tosinadebayo55@gmail.com"),
        help="Tenant admin email used for login",
    )
    parser.add_argument(
        "--password",
        default=_env("LURAN_SMOKE_PASSWORD", "Test1234$"),
        help="Tenant admin password used for login",
    )
    parser.add_argument(
        "--order-flow-name",
        default=_env("LURAN_SMOKE_ORDER_FLOW_NAME", "Automation Flow Smoke Success e00be652"),
        help="Exact flow name for the action_call smoke path",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(_env("LURAN_SMOKE_TIMEOUT_SECONDS", "60")),
        help="HTTP timeout for chat requests",
    )
    parser.add_argument(
        "--suffix",
        default=_env("LURAN_SMOKE_SUFFIX", "regression"),
        help="Suffix used for generated smoke user identifiers",
    )
    args = parser.parse_args()
    return SmokeConfig(
        base_url=args.base_url.rstrip("/"),
        tenant_id=args.tenant_id,
        email=args.email,
        password=args.password,
        order_flow_name=args.order_flow_name,
        timeout_seconds=args.timeout_seconds,
        suffix=args.suffix,
    )


CONFIG = parse_args()


def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def smoke_user(name: str) -> str:
    return f"{name}-{CONFIG.suffix}@example.com"


def login() -> str:
    r = requests.post(
        f"{CONFIG.base_url}/auth/login",
        json={"email": CONFIG.email, "password": CONFIG.password},
        timeout=30,
    )
    expect(r.status_code == 200, f"Login failed: {r.status_code} {r.text}")
    data = r.json()
    expect("access_token" in data, f"No access_token in login response: {data}")
    return data["access_token"]


def authed_get(path: str, token: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{CONFIG.base_url}{path}", headers=headers, timeout=30, **kwargs)


def post_chat(message: str, user_identifier: str, session_id: str | None = None):
    payload = {"message": message, "user_identifier": user_identifier}
    if session_id:
        payload["session_id"] = session_id
    else:
        payload["tenant_id"] = CONFIG.tenant_id
    r = requests.post(
        f"{CONFIG.base_url}/chat/message",
        json=payload,
        timeout=CONFIG.timeout_seconds,
    )
    expect(r.status_code == 200, f"Chat request failed: {r.status_code} {r.text}")
    return r.json()


def main() -> int:
    print("\n=== LOGIN ===")
    token = login()
    print("Logged in successfully.")

    print("\n=== ACTION INVENTORY ===")
    actions_r = authed_get("/tenant/automation/actions", token)
    expect(
        actions_r.status_code == 200,
        f"List actions failed: {actions_r.status_code} {actions_r.text}",
    )
    actions = actions_r.json()
    action_by_slug = {a["slug"]: a for a in actions}
    for slug in [
        "track_order_status_chat",
        "lookup_account_by_email_chat",
        "check_account_status_chat",
        "unlock_suspended_account_chat",
    ]:
        expect(slug in action_by_slug, f"Missing expected action: {slug}")
    expect(
        action_by_slug["track_order_status_chat"]["available_in_ai_chat"] is True,
        "Track order action not available in AI chat",
    )
    expect(
        action_by_slug["check_account_status_chat"]["available_in_ai_chat"] is True,
        "Check account status not available in AI chat",
    )
    expect(
        action_by_slug["unlock_suspended_account_chat"]["requires_approval"] is True,
        "Unlock action should require approval",
    )
    print("Expected actions are present and AI-chat eligibility looks correct.")

    print("\n=== FLOW INVENTORY ===")
    flows_r = authed_get("/flows", token, params={"tenant_id": CONFIG.tenant_id})
    expect(flows_r.status_code == 200, f"List flows failed: {flows_r.status_code} {flows_r.text}")
    flows_payload = flows_r.json()
    flows = flows_payload["flows"]
    target_flow = next((f for f in flows if f["name"] == CONFIG.order_flow_name), None)
    expect(target_flow is not None, f"Missing expected flow: {CONFIG.order_flow_name}")
    expect(target_flow["status"] == "active", f'Flow not active: {target_flow["status"]}')
    print(f'Found active smoke flow: {target_flow["id"]}')

    print("\n=== AI CHAT: TRACK ORDER SUCCESS ===")
    user = smoke_user("smoke-order-success")
    step1 = post_chat("I want to track my order", user)
    session_id = step1["session_id"]
    expect(
        "order" in step1["message"]["content"].lower(),
        f'Expected order prompt, got: {step1["message"]["content"]}',
    )
    step2 = post_chat("ORD1234", user, session_id=session_id)
    success_text = step2["message"]["content"]
    expect(
        "processing" in success_text.lower(),
        f"Expected processing in success reply, got: {success_text}",
    )
    print("AI chat order tracking success path passed.")

    print("\n=== AI CHAT: TRACK ORDER INVALID ===")
    user = smoke_user("smoke-order-failure")
    step1 = post_chat("Track my order", user)
    step2 = post_chat("ORD12", user, session_id=step1["session_id"])
    invalid_text = step2["message"]["content"]
    expect(
        "could not find" in invalid_text.lower() or "double-check" in invalid_text.lower(),
        f"Expected not-found style reply, got: {invalid_text}",
    )
    print("AI chat invalid order path passed.")

    print("\n=== AI CHAT: ACCOUNT STATUS LOOKUP CHAIN ===")
    user = smoke_user("smoke-account-status")
    step1 = post_chat("I cannot log into my account, can you check the status?", user)
    expect(
        "email" in step1["message"]["content"].lower(),
        f'Expected email prompt, got: {step1["message"]["content"]}',
    )
    step2 = post_chat("tosinadebayo55@gmail.com", user, session_id=step1["session_id"])
    account_text = step2["message"]["content"]
    expect("active" in account_text.lower(), f"Expected active account reply, got: {account_text}")
    print("AI chat account lookup chain passed.")

    print("\n=== AI CHAT: APPROVAL-REQUIRED UNLOCK CHAIN ===")
    user = smoke_user("smoke-account-unlock")
    step1 = post_chat("My account is suspended and I need it unlocked", user)
    expect(
        "email" in step1["message"]["content"].lower(),
        f'Expected email prompt, got: {step1["message"]["content"]}',
    )
    step2 = post_chat("suspuser@example.com", user, session_id=step1["session_id"])
    expect(
        "reason" in step2["message"]["content"].lower(),
        f'Expected reason prompt, got: {step2["message"]["content"]}',
    )
    step3 = post_chat(
        "I was locked out after too many login attempts",
        user,
        session_id=step1["session_id"],
    )
    expect(step3.get("should_escalate") is True, f"Expected escalation flag, got: {pretty(step3)}")
    expect(
        "approval" in (step3.get("escalation_reason") or "").lower(),
        f'Expected approval escalation reason, got: {step3.get("escalation_reason")}',
    )
    print("AI chat approval-required chain passed.")

    print("\n=== FLOW: ACTION_CALL SUCCESS PATH ===")
    flow_user = smoke_user("smoke-flow-order")
    start = post_chat(f'__flow_start__:{target_flow["id"]}', flow_user)
    start_text = start["message"]["content"]
    expect("order" in start_text.lower(), f"Expected flow input prompt, got: {start_text}")
    flow_session = start["session_id"]
    done = post_chat("ORD1234", flow_user, session_id=flow_session)
    done_text = done["message"]["content"]
    expect(
        "processing" in done_text.lower() and "eta" in done_text.lower(),
        f"Expected interpolated flow end message, got: {done_text}",
    )
    print("Flow action_call success path passed.")

    print("\n=== FLOW EXECUTION DATA CHECK ===")
    execs_r = authed_get(
        f'/flows/{target_flow["id"]}/executions',
        token,
        params={"limit": 5, "offset": 0},
    )
    expect(
        execs_r.status_code == 200,
        f"List flow executions failed: {execs_r.status_code} {execs_r.text}",
    )
    execs = execs_r.json()["executions"]
    latest = next((e for e in execs if e["session_id"] == flow_session), None)
    expect(latest is not None, f"Could not find flow execution for session {flow_session}")
    collected = latest.get("collected_data") or {}
    expect(
        collected.get("customer_order_number") == "ORD1234",
        f"Expected saved input variable, got: {pretty(collected)}",
    )
    expect(
        collected.get("final_order_status") == "processing",
        f"Expected final_order_status=processing, got: {pretty(collected)}",
    )
    expect(
        "final_eta_date" in collected,
        f"Expected final_eta_date in collected_data, got: {pretty(collected)}",
    )
    print("Flow execution saved data looks correct.")

    print("\n=== SUMMARY ===")
    print("All smoke checks passed:")
    print("- tenant automation inventory")
    print("- AI chat direct action success/failure")
    print("- AI chat lookup chain")
    print("- AI chat approval-required action")
    print("- flow action_call execution")
    print("- flow saved output mapping and interpolation")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("\nSMOKE TEST FAILED")
        print(str(exc))
        raise
