"""Real-account end-to-end enforcement check for automation, flows, and plan gates.

This script uses a real tenant admin account against the live local API.
It exercises:
- login and tenant/subscription endpoints
- AI chat automation behavior
- plan downgrade/restore
- lookup-chain gating on Lite
- webhook gating on Lite
- webhook count limit on Professional
- automation action count limit on Lite
- automation execution limit via temporary capability override

The script restores the tenant's original plan and temporary overrides before exiting.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import requests

from app.db.models.tenant import TenantCapabilities
from app.db.session import SessionLocal

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@dataclass
class Config:
    base_url: str
    email: str
    password: str
    timeout_seconds: int


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Run real-account E2E enforcement checks.")
    parser.add_argument(
        "--base-url",
        default=_env("LURAN_E2E_BASE_URL", "http://localhost:8000/api/v1"),
    )
    parser.add_argument(
        "--email",
        default=_env("LURAN_E2E_EMAIL", "tosinadebayo55@gmail.com"),
    )
    parser.add_argument(
        "--password",
        default=_env("LURAN_E2E_PASSWORD", "Test1234$"),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(_env("LURAN_E2E_TIMEOUT_SECONDS", "60")),
    )
    args = parser.parse_args()
    return Config(
        base_url=args.base_url.rstrip("/"),
        email=args.email,
        password=args.password,
        timeout_seconds=args.timeout_seconds,
    )


CONFIG = parse_args()


def pretty(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class ApiClient:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.token: str | None = None

    def login(self) -> None:
        response = self.session.post(
            f"{self.config.base_url}/auth/login",
            json={"email": self.config.email, "password": self.config.password},
            timeout=30,
        )
        expect(response.status_code == 200, f"Login failed: {response.status_code} {response.text}")
        payload = response.json()
        self.token = payload["access_token"]
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(f"{self.config.base_url}{path}", timeout=30, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.config.timeout_seconds)
        return self.session.post(f"{self.config.base_url}{path}", timeout=timeout, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self.session.delete(f"{self.config.base_url}{path}", timeout=30, **kwargs)


def post_chat(
    client: ApiClient,
    *,
    tenant_id: str,
    user_identifier: str,
    message: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": message, "user_identifier": user_identifier}
    if session_id:
        payload["session_id"] = session_id
    else:
        payload["tenant_id"] = tenant_id

    response = client.post("/chat/message", json=payload)
    expect(response.status_code == 200, f"Chat failed: {response.status_code} {response.text}")
    return response.json()


def get_json(response: requests.Response, *, expected_status: int = 200) -> Any:
    expect(
        response.status_code == expected_status,
        f"Unexpected status {response.status_code}: {response.text}",
    )
    return response.json()


@contextmanager
def capability_override(tenant_id: str, **overrides: Any):
    db = SessionLocal()
    original: dict[str, Any] = {}
    try:
        row = (
            db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == UUID(tenant_id))
            .one()
        )
        for field, value in overrides.items():
            original[field] = getattr(row, field)
            setattr(row, field, value)
        db.commit()
        yield
    finally:
        row = (
            db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == UUID(tenant_id))
            .one()
        )
        for field, value in original.items():
            setattr(row, field, value)
        db.commit()
        db.close()


def build_webhook_flow_payload(tenant_id: str, *, name: str, webhook_count: int) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for index in range(webhook_count):
        node_id = f"webhook_{index + 1}"
        next_node_id = "flow_end" if index == webhook_count - 1 else f"webhook_{index + 2}"
        nodes.append(
            {
                "id": node_id,
                "type": "webhook",
                "config": {
                    "url": f"https://example.test/{node_id}",
                    "method": "POST",
                    "next_node_id": next_node_id,
                },
            }
        )
    nodes.append({"id": "flow_end", "type": "end", "config": {"message": "Done"}})

    return {
        "tenant_id": tenant_id,
        "name": name,
        "description": "E2E webhook gating check",
        "trigger_type": "manual",
        "trigger_config": {},
        "status": "draft",
        "is_active": True,
        "version": 1,
        "nodes": {"start_node_id": "webhook_1", "nodes": nodes, "edges": []},
    }


def build_simple_flow_payload(tenant_id: str, *, name: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "name": name,
        "description": "Temporary E2E flow for delete verification",
        "trigger_type": "manual",
        "trigger_config": {},
        "status": "draft",
        "is_active": True,
        "version": 1,
        "nodes": {
            "start_node_id": "end",
            "nodes": [{"id": "end", "type": "end", "config": {"message": "Done"}}],
            "edges": [],
        },
    }


def build_temp_action_payload(name: str, slug: str) -> dict[str, Any]:
    return {
        "name": name,
        "slug": slug,
        "description": "Temporary E2E action for limit verification",
        "status": "active",
        "type": "http",
        "method": "GET",
        "url_template": "http://host.docker.internal:8000/api/v1/automation-sandbox/orders/status",
        "timeout_seconds": 20,
        "auth_type": "none",
        "auth_config": {},
        "headers_template": {},
        "query_template": {"order_reference": "{{order_reference}}"},
        "body_template": {},
        "input_schema": {"required": ["order_reference"]},
        "output_schema": {},
        "error_mapping": {},
        "retry_policy": {"max_attempts": 1, "backoff_ms": 0},
        "is_sensitive": False,
        "requires_approval": False,
        "allow_in_flows": False,
        "available_in_ai_chat": False,
    }


def main() -> int:
    client = ApiClient(CONFIG)
    created_action_ids: list[str] = []
    tenant_id: str | None = None

    try:
        print("\n=== LOGIN ===")
        client.login()
        print("Logged in successfully.")

        print("\n=== TENANT + SUBSCRIPTION ===")
        me = get_json(client.get("/auth/me"))
        tenant = get_json(client.get("/tenant"))
        tenant_id = tenant["id"]
        subscription = get_json(client.get("/subscription/my-subscription"))
        capabilities = get_json(client.get("/tenant/capabilities"))
        original_tier = subscription["subscription_tier"]
        print(
            pretty(
                {
                    "email": me["email"],
                    "tenant_id": tenant_id,
                    "subscription_tier": original_tier,
                    "subscription_status": subscription["subscription_status"],
                    "capabilities": {
                        "max_automation_actions": capabilities.get("max_automation_actions"),
                        "max_automation_executions_per_month": capabilities.get(
                            "max_automation_executions_per_month"
                        ),
                        "can_use_ai_chat_automation": capabilities.get(
                            "can_use_ai_chat_automation"
                        ),
                        "can_use_lookup_chains": capabilities.get("can_use_lookup_chains"),
                        "max_flows": capabilities.get("max_flows"),
                    },
                }
            )
        )

        print("\n=== BASELINE AI CHAT AUTOMATION ===")
        baseline_user = f"real-e2e-baseline-{int(time.time())}@example.com"
        first = post_chat(
            client,
            tenant_id=tenant_id,
            user_identifier=baseline_user,
            message="I want to track my order",
        )
        expect(
            "order" in first["message"]["content"].lower(),
            f"Expected order prompt, got: {first['message']['content']}",
        )
        second = post_chat(
            client,
            tenant_id=tenant_id,
            user_identifier=baseline_user,
            message="ORD1234",
            session_id=first["session_id"],
        )
        expect(
            "processing" in second["message"]["content"].lower(),
            f"Expected successful order status reply, got: {second['message']['content']}",
        )
        print("Baseline AI chat automation is working.")

        print("\n=== ACTION + FLOW INVENTORY ===")
        actions = get_json(client.get("/tenant/automation/actions"))
        flows = get_json(client.get("/flows", params={"tenant_id": tenant_id}))["flows"]
        print(
            pretty(
                {
                    "action_count": len(actions),
                    "flow_count": len(flows),
                    "action_slugs": [a["slug"] for a in actions],
                }
            )
        )
        track_action = next(a for a in actions if a["slug"] == "track_order_status_chat")

        if original_tier not in {"professional", "growth", "enterprise"}:
            raise AssertionError(f"Unexpected subscription tier for E2E plan test: {original_tier}")

        print("\n=== DOWNGRADE TO LITE IS BLOCKED WHEN USAGE EXCEEDS LIMITS ===")
        downgrade_attempt = client.post(
            "/subscription/downgrade",
            json={"tier": "lite", "reason": "Automated E2E plan gate verification"},
        )
        expect(
            downgrade_attempt.status_code == 400,
            f"Expected downgrade validation failure, got: {downgrade_attempt.status_code} {downgrade_attempt.text}",
        )
        downgrade_payload = downgrade_attempt.json()
        expect("detail" in downgrade_payload, f"Unexpected downgrade payload: {downgrade_payload}")
        downgrade_detail = downgrade_payload["detail"]
        expect(
            isinstance(downgrade_detail, dict) and downgrade_detail.get("error") == "data_exceeds_limits",
            f"Expected structured downgrade violation payload, got: {pretty(downgrade_payload)}",
        )
        violation_fields = {item.get("field") for item in downgrade_detail.get("violations", [])}
        expect("flows" in violation_fields, f"Expected flow limit violation, got: {pretty(downgrade_payload)}")
        print("Downgrade validation now blocks over-limit tenants correctly.")

        print("\n=== LITE-LIKE CAPABILITY OVERRIDES ON REAL TENANT ===")
        with capability_override(
            tenant_id,
            max_automation_actions=5,
            max_automation_executions_per_month=500,
            can_use_lookup_chains=False,
            can_use_ai_chat_automation=True,
            can_use_webhooks=False,
            max_flows=len(flows) + 5,
            max_webhooks_per_flow=0,
        ):
            lite_caps = get_json(client.get("/tenant/capabilities"))
            expect(lite_caps["max_automation_actions"] == 5, pretty(lite_caps))
            expect(lite_caps["max_automation_executions_per_month"] == 500, pretty(lite_caps))
            expect(lite_caps["can_use_ai_chat_automation"] is True, pretty(lite_caps))
            expect(lite_caps["can_use_lookup_chains"] is False, pretty(lite_caps))
            expect(lite_caps["can_use_webhooks"] is False, pretty(lite_caps))
            expect(lite_caps["max_webhooks_per_flow"] == 0, pretty(lite_caps))
            print("Tenant capability override reflects Lite-like automation limits.")

            print("\n=== LITE-LIKE: LOOKUP CHAINS BLOCKED ===")
            lite_lookup_user = f"real-e2e-lite-lookup-{int(time.time())}@example.com"
            lookup_step = post_chat(
                client,
                tenant_id=tenant_id,
                user_identifier=lite_lookup_user,
                message="Can you check my account status?",
            )
            lookup_text = lookup_step["message"]["content"].lower()
            expect("account" in lookup_text, f"Expected account-related prompt, got: {lookup_text}")
            expect("email" not in lookup_text, f"Lookup chain should be blocked: {lookup_text}")
            print("Lookup-chain gating is working.")

            print("\n=== LITE-LIKE: WEBHOOK NODES BLOCKED ===")
            lite_webhook_flow = build_webhook_flow_payload(
                tenant_id,
                name=f"E2E Lite-Like Webhook Block {int(time.time())}",
                webhook_count=1,
            )
            lite_webhook_response = client.post("/flows", json=lite_webhook_flow)
            expect(
                lite_webhook_response.status_code == 402,
                f"Expected webhook block, got: {lite_webhook_response.status_code} {lite_webhook_response.text}",
            )
            expect(
                "webhook" in lite_webhook_response.text.lower(),
                f"Expected webhook-related message, got: {lite_webhook_response.text}",
            )
            print("Webhook gating is working.")

            print("\n=== LITE-LIKE: AUTOMATION ACTION COUNT LIMIT ===")
            current_actions = get_json(client.get("/tenant/automation/actions"))
            limit_hit = False
            attempts = 0
            while attempts < 3 and not limit_hit:
                attempts += 1
                suffix = f"{int(time.time())}-{attempts}"
                payload = build_temp_action_payload(
                    name=f"E2E Temp Action {suffix}",
                    slug=f"e2e-temp-action-{suffix}",
                )
                create_response = client.post("/tenant/automation/actions", json=payload)
                if create_response.status_code == 201:
                    created_action = create_response.json()
                    created_action_ids.append(created_action["id"])
                    continue
                expect(
                    create_response.status_code == 403,
                    f"Expected action limit block, got: {create_response.status_code} {create_response.text}",
                )
                expect(
                    "automation action limit reached" in create_response.text.lower(),
                    f"Expected action-limit error, got: {create_response.text}",
                )
                limit_hit = True
            expect(limit_hit, "Did not hit the Lite-like automation action limit as expected")
            print("Automation action count limit is working.")

            print("\n=== LITE-LIKE: EXECUTION LIMIT BLOCKED VIA CAPABILITY OVERRIDE ===")
            with capability_override(tenant_id, max_automation_executions_per_month=0):
                blocked_test = client.post(
                    f"/tenant/automation/actions/{track_action['id']}/test",
                    json={"input_payload": {"order_reference": "ORD1234"}},
                )
                expect(
                    blocked_test.status_code == 200,
                    f"Expected action test response, got: {blocked_test.status_code} {blocked_test.text}",
                )
                blocked_payload = blocked_test.json()
                expect(
                    blocked_payload["status"] == "blocked_policy",
                    f"Expected blocked_policy, got: {pretty(blocked_payload)}",
                )
                expect(
                    blocked_payload["error_code"] == "AUTOMATION_EXECUTION_LIMIT_REACHED",
                    f"Expected execution-limit code, got: {pretty(blocked_payload)}",
                )
            print("Execution-limit enforcement is working.")

        print(f"\n=== {original_tier.upper()}: WEBHOOK COUNT LIMIT ===")
        count_flow = build_webhook_flow_payload(
            tenant_id,
            name=f"E2E Webhook Count Block {int(time.time())}",
            webhook_count=4,
        )
        count_response = client.post("/flows", json=count_flow)
        expect(
            count_response.status_code == 402,
            f"Expected webhook count block, got: {count_response.status_code} {count_response.text}",
        )
        expect(
            "webhook node limit reached" in count_response.text.lower(),
            f"Expected webhook count error, got: {count_response.text}",
        )
        print("Webhook count limit is working.")

        print("\n=== FLOW DELETE IS HARD DELETE ===")
        temp_flow_response = client.post(
            "/flows",
            json=build_simple_flow_payload(tenant_id, name=f"E2E Delete Flow {int(time.time())}"),
        )
        expect(
            temp_flow_response.status_code == 201,
            f"Expected temp flow create success, got: {temp_flow_response.status_code} {temp_flow_response.text}",
        )
        temp_flow = temp_flow_response.json()
        delete_response = client.delete(f"/flows/{temp_flow['id']}")
        expect(
            delete_response.status_code == 204,
            f"Expected flow delete success, got: {delete_response.status_code} {delete_response.text}",
        )
        deleted_lookup = client.get(f"/flows/{temp_flow['id']}")
        expect(
            deleted_lookup.status_code == 404,
            f"Expected deleted flow to be gone, got: {deleted_lookup.status_code} {deleted_lookup.text}",
        )
        print("Flow deletion now removes the flow permanently.")

        print("\n=== FINAL AI CHAT + FLOW SANITY ===")
        final_user = f"real-e2e-final-{int(time.time())}@example.com"
        order_first = post_chat(
            client,
            tenant_id=tenant_id,
            user_identifier=final_user,
            message="what is my order status?",
        )
        expect(
            "order" in order_first["message"]["content"].lower(),
            f"Expected final order prompt, got: {order_first['message']['content']}",
        )
        order_second = post_chat(
            client,
            tenant_id=tenant_id,
            user_identifier=final_user,
            message="ORD1234",
            session_id=order_first["session_id"],
        )
        expect(
            "processing" in order_second["message"]["content"].lower(),
            f"Expected final order success, got: {order_second['message']['content']}",
        )
        print("Final AI chat sanity passed.")

        print("\n=== SUMMARY ===")
        print("- Real account login and tenant endpoints passed")
        print("- Baseline AI chat automation passed")
        print("- Downgrade-to-lite validation now blocks over-limit tenants")
        print("- Lite-like lookup-chain gating passed on the real tenant")
        print("- Lite-like webhook gating passed on the real tenant")
        print("- Lite-like automation action count limit passed on the real tenant")
        print("- Automation execution limit passed")
        print("- Webhook count limit passed")
        print("- Flow deletion now removes flows permanently")
        print("- Final AI chat sanity passed")
        return 0

    finally:
        if created_action_ids and client.token:
            for action_id in created_action_ids:
                try:
                    client.delete(f"/tenant/automation/actions/{action_id}")
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("\nE2E TEST FAILED")
        print(str(exc))
        raise
