"""Lightweight chat-to-automation orchestration for tenant actions."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.automation import (
    ActionExecutionStatus,
    ActionTriggerSource,
    TenantAction,
    TenantActionStatus,
)
from app.schemas.conversation import MessageResponse
from app.db.models.tenant import TenantCapabilities
from app.services.automation_service import AutomationService
from app.services.conversation_service import ConversationService
from app.services.rag_service import RAGService

PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.@+\-#:/ ]{1,200}$")
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

ACTION_HINT_WORDS = {
    "book",
    "booking",
    "schedule",
    "appointment",
    "track",
    "check",
    "lookup",
    "find",
    "create",
    "open",
    "submit",
    "status",
    "unlock",
    "reactivate",
    "update",
    "cancel",
    "refund",
    "order",
    "account",
    "ticket",
}
CUSTOMER_FIELD_HINTS = (
    "account",
    "date",
    "email",
    "id",
    "issue",
    "message",
    "name",
    "number",
    "order",
    "phone",
    "reason",
    "reference",
    "subject",
    "time",
    "ticket",
)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "for",
    "get",
    "help",
    "i",
    "is",
    "me",
    "my",
    "of",
    "please",
    "the",
    "to",
    "what",
    "with",
    "you",
}


@dataclass
class AutomationTurnResult:
    """Result of attempting chat-side automation orchestration."""

    handled: bool
    assistant_text: str | None = None
    token_count: int | None = 0
    metadata: dict[str, Any] | None = None
    should_escalate: bool = False
    escalation_reason: str | None = None


@dataclass
class LookupPlan:
    """Represents a one-step lookup needed before the target action can run."""

    lookup_action_id: str
    target_field: str
    lookup_required_fields: list[str]
    remaining_target_fields: list[str]


class ChatAutomationOrchestrator:
    """Detects, gathers inputs for, and executes tenant actions from chat."""

    LLM_REPLY_TIMEOUT_SECONDS = 12

    def __init__(self, db: Session):
        self.db = db
        self.automation_service = AutomationService(db)
        self.conversation_service = ConversationService(db)

    def _can_use_ai_chat_automation(self, tenant_id: UUID) -> bool:
        capabilities = (
            self.db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == tenant_id)
            .one_or_none()
        )
        if not capabilities:
            return False
        return bool(capabilities.can_use_ai_chat_automation)

    def _can_use_lookup_chains(self, tenant_id: UUID) -> bool:
        capabilities = (
            self.db.query(TenantCapabilities)
            .filter(TenantCapabilities.tenant_id == tenant_id)
            .one_or_none()
        )
        if not capabilities:
            return False
        return bool(capabilities.can_use_lookup_chains)

    async def handle_turn(
        self,
        *,
        tenant_id: UUID,
        session_id: UUID,
        user_message: str,
        session_meta: dict[str, Any],
        conversation_history: list[MessageResponse],
        tenant_ai_config: Any | None = None,
        company_name: str | None = None,
    ) -> AutomationTurnResult:
        pending = session_meta.get("pending_ai_action")
        if isinstance(pending, dict) and pending.get("action_id"):
            if not self._can_use_ai_chat_automation(tenant_id):
                session_meta.pop("pending_ai_action", None)
                self.conversation_service.update_session_meta(session_id, session_meta)
                return AutomationTurnResult(handled=False)
            return await self._continue_pending_action(
                tenant_id=tenant_id,
                session_id=session_id,
                user_message=user_message,
                session_meta=session_meta,
                pending=pending,
                tenant_ai_config=tenant_ai_config,
                company_name=company_name,
            )

        actions = [
            action
            for action in self.automation_service.list_actions(tenant_id)
            if action.status == TenantActionStatus.ACTIVE and action.available_in_ai_chat
        ]
        if actions and not self._can_use_ai_chat_automation(tenant_id):
            return AutomationTurnResult(handled=False)
        if not actions:
            return AutomationTurnResult(handled=False)

        matched = self._match_action(user_message=user_message, actions=actions)
        if not matched:
            return AutomationTurnResult(handled=False)

        action, score = matched
        if score < 2:
            return AutomationTurnResult(handled=False)

        required_fields = self._extract_required_fields(action)
        collected_inputs = self._extract_current_inputs(required_fields, user_message)
        missing_fields = [field for field in required_fields if field not in collected_inputs]
        if missing_fields:
            lookup_plan = self._build_lookup_plan(
                tenant_id=tenant_id,
                target_action=action,
                actions=actions,
                target_missing_fields=missing_fields,
            )
            if lookup_plan:
                missing_lookup_fields = [
                    field
                    for field in lookup_plan.lookup_required_fields
                    if field not in collected_inputs
                ]
                if missing_lookup_fields:
                    session_meta["pending_ai_action"] = {
                        "action_id": str(action.id),
                        "action_name": action.name,
                        "collected_inputs": collected_inputs,
                        "missing_fields": missing_lookup_fields,
                        "lookup_plan": {
                            "lookup_action_id": lookup_plan.lookup_action_id,
                            "target_field": lookup_plan.target_field,
                            "lookup_required_fields": lookup_plan.lookup_required_fields,
                            "remaining_target_fields": lookup_plan.remaining_target_fields,
                        },
                    }
                    self.conversation_service.update_session_meta(session_id, session_meta)
                    prompt_text = await self._generate_missing_field_prompt(
                        action=action,
                        field_name=missing_lookup_fields[0],
                        tenant_ai_config=tenant_ai_config,
                        company_name=company_name,
                        fallback_action_name=action.name,
                    )
                    return AutomationTurnResult(
                        handled=True,
                        assistant_text=prompt_text,
                        metadata={
                            "automation_orchestrated": True,
                            "automation_action_id": str(action.id),
                            "automation_action_name": action.name,
                            "automation_mode": "collect_input",
                            "missing_field": missing_lookup_fields[0],
                            "lookup_target_field": lookup_plan.target_field,
                        },
                    )

            prompt = await self._generate_missing_field_prompt(
                action=action,
                field_name=missing_fields[0],
                tenant_ai_config=tenant_ai_config,
                company_name=company_name,
                fallback_action_name=action.name,
            )
            session_meta["pending_ai_action"] = {
                "action_id": str(action.id),
                "action_name": action.name,
                "collected_inputs": collected_inputs,
                "missing_fields": missing_fields,
            }
            self.conversation_service.update_session_meta(session_id, session_meta)
            return AutomationTurnResult(
                handled=True,
                assistant_text=prompt,
                metadata={
                    "automation_orchestrated": True,
                    "automation_action_id": str(action.id),
                    "automation_action_name": action.name,
                    "automation_mode": "collect_input",
                    "missing_field": missing_fields[0],
                },
            )

        return await self._execute_action_from_chat(
            tenant_id=tenant_id,
            session_id=session_id,
            action=action,
            input_payload=collected_inputs,
            session_meta=session_meta,
            tenant_ai_config=tenant_ai_config,
            company_name=company_name,
        )

    async def _continue_pending_action(
        self,
        *,
        tenant_id: UUID,
        session_id: UUID,
        user_message: str,
        session_meta: dict[str, Any],
        pending: dict[str, Any],
        tenant_ai_config: Any | None,
        company_name: str | None,
    ) -> AutomationTurnResult:
        action_id_raw = pending.get("action_id")
        missing_fields = [
            str(field) for field in pending.get("missing_fields", []) if isinstance(field, str)
        ]
        collected_inputs = dict(pending.get("collected_inputs") or {})
        lookup_plan = (
            pending.get("lookup_plan") if isinstance(pending.get("lookup_plan"), dict) else None
        )

        if not action_id_raw or not missing_fields:
            session_meta.pop("pending_ai_action", None)
            self.conversation_service.update_session_meta(session_id, session_meta)
            return AutomationTurnResult(handled=False)

        if user_message.strip().lower() in {"cancel", "stop", "never mind", "nevermind"}:
            session_meta.pop("pending_ai_action", None)
            self.conversation_service.update_session_meta(session_id, session_meta)
            return AutomationTurnResult(
                handled=True,
                assistant_text="No problem. I have cancelled that automation request.",
                metadata={
                    "automation_orchestrated": True,
                    "automation_mode": "cancelled",
                },
            )

        action = self.automation_service.get_action(
            tenant_id=tenant_id, action_id=UUID(action_id_raw)
        )
        if not action or action.status != TenantActionStatus.ACTIVE:
            session_meta.pop("pending_ai_action", None)
            self.conversation_service.update_session_meta(session_id, session_meta)
            return AutomationTurnResult(handled=False)

        field_name = missing_fields[0]
        extracted_value = self._extract_field_value(field_name, user_message)
        if extracted_value is None:
            invalid_prompt = await self._generate_invalid_field_prompt(
                action=action,
                field_name=field_name,
                tenant_ai_config=tenant_ai_config,
                company_name=company_name,
            )
            return AutomationTurnResult(
                handled=True,
                assistant_text=invalid_prompt,
                metadata={
                    "automation_orchestrated": True,
                    "automation_action_id": str(action.id),
                    "automation_action_name": action.name,
                    "automation_mode": "invalid_input",
                    "missing_field": field_name,
                },
            )

        collected_inputs[field_name] = extracted_value
        session_meta[field_name] = extracted_value
        remaining_fields = missing_fields[1:]

        if remaining_fields:
            session_meta["pending_ai_action"] = {
                "action_id": str(action.id),
                "action_name": action.name,
                "collected_inputs": collected_inputs,
                "missing_fields": remaining_fields,
            }
            self.conversation_service.update_session_meta(session_id, session_meta)
            prompt_text = await self._generate_missing_field_prompt(
                action=action,
                field_name=remaining_fields[0],
                tenant_ai_config=tenant_ai_config,
                company_name=company_name,
                fallback_action_name=action.name,
            )
            return AutomationTurnResult(
                handled=True,
                assistant_text=prompt_text,
                metadata={
                    "automation_orchestrated": True,
                    "automation_action_id": str(action.id),
                    "automation_action_name": action.name,
                    "automation_mode": "collect_input",
                    "missing_field": remaining_fields[0],
                },
            )

        if lookup_plan:
            lookup_action = self.automation_service.get_action(
                tenant_id=tenant_id,
                action_id=UUID(str(lookup_plan["lookup_action_id"])),
            )
            if not lookup_action or lookup_action.status != TenantActionStatus.ACTIVE:
                session_meta.pop("pending_ai_action", None)
                self.conversation_service.update_session_meta(session_id, session_meta)
                return AutomationTurnResult(
                    handled=True,
                    assistant_text="I could not continue because the lookup step is no longer available.",
                    metadata={
                        "automation_orchestrated": True,
                        "automation_mode": "lookup_unavailable",
                    },
                )

            lookup_payload = {
                field: collected_inputs[field]
                for field in lookup_plan.get("lookup_required_fields", [])
                if field in collected_inputs
            }
            resolved_value = await self._resolve_lookup_target_value(
                tenant_id=tenant_id,
                session_id=session_id,
                lookup_action=lookup_action,
                target_field=str(lookup_plan["target_field"]),
                input_payload=lookup_payload,
            )
            if not resolved_value:
                session_meta.pop("pending_ai_action", None)
                self.conversation_service.update_session_meta(session_id, session_meta)
                return AutomationTurnResult(
                    handled=True,
                    assistant_text=self._build_lookup_failure_prompt(
                        str(lookup_plan["target_field"])
                    ),
                    metadata={
                        "automation_orchestrated": True,
                        "automation_action_id": str(action.id),
                        "automation_action_name": action.name,
                        "automation_mode": "lookup_failed",
                        "lookup_action_id": str(lookup_action.id),
                    },
                )

            collected_inputs[str(lookup_plan["target_field"])] = resolved_value
            session_meta[str(lookup_plan["target_field"])] = resolved_value
            remaining_target_fields = [
                field
                for field in lookup_plan.get("remaining_target_fields", [])
                if field not in collected_inputs
            ]
            if remaining_target_fields:
                session_meta["pending_ai_action"] = {
                    "action_id": str(action.id),
                    "action_name": action.name,
                    "collected_inputs": collected_inputs,
                    "missing_fields": remaining_target_fields,
                }
                self.conversation_service.update_session_meta(session_id, session_meta)
                prompt_text = await self._generate_missing_field_prompt(
                    action=action,
                    field_name=remaining_target_fields[0],
                    tenant_ai_config=tenant_ai_config,
                    company_name=company_name,
                    fallback_action_name=action.name,
                )
                return AutomationTurnResult(
                    handled=True,
                    assistant_text=prompt_text,
                    metadata={
                        "automation_orchestrated": True,
                        "automation_action_id": str(action.id),
                        "automation_action_name": action.name,
                        "automation_mode": "collect_input",
                        "missing_field": remaining_target_fields[0],
                    },
                )

        session_meta.pop("pending_ai_action", None)
        self.conversation_service.update_session_meta(session_id, session_meta)
        return await self._execute_action_from_chat(
            tenant_id=tenant_id,
            session_id=session_id,
            action=action,
            input_payload=collected_inputs,
            session_meta=session_meta,
            tenant_ai_config=tenant_ai_config,
            company_name=company_name,
        )

    async def _execute_action_from_chat(
        self,
        *,
        tenant_id: UUID,
        session_id: UUID,
        action: TenantAction,
        input_payload: dict[str, Any],
        session_meta: dict[str, Any],
        tenant_ai_config: Any | None,
        company_name: str | None,
    ) -> AutomationTurnResult:
        execution = await self.automation_service.execute_action(
            tenant_id=tenant_id,
            action=action,
            input_payload=input_payload,
            session_id=session_id,
            trigger_source=ActionTriggerSource.AI_CHAT,
        )
        response_text, should_escalate, escalation_reason = await self._format_execution_response(
            action=action,
            execution=execution,
            tenant_ai_config=tenant_ai_config,
            company_name=company_name,
            input_payload=input_payload,
        )

        if should_escalate:
            session_meta["pending_escalation"] = True
            session_meta["escalation_context"] = {
                "reason": escalation_reason,
                "original_query": action.name,
            }
            self.conversation_service.update_session_meta(session_id, session_meta)

        return AutomationTurnResult(
            handled=True,
            assistant_text=response_text,
            metadata={
                "automation_orchestrated": True,
                "automation_action_id": str(action.id),
                "automation_action_name": action.name,
                "automation_execution_id": str(execution.id),
                "automation_status": execution.status.value,
            },
            should_escalate=should_escalate,
            escalation_reason=escalation_reason,
        )

    def _match_action(
        self, *, user_message: str, actions: list[TenantAction]
    ) -> tuple[TenantAction, int] | None:
        tokens = self._tokenize(user_message)
        if not tokens or not (set(tokens) & ACTION_HINT_WORDS):
            return None

        best: tuple[TenantAction, int] | None = None
        for action in actions:
            action_tokens = self._tokenize(
                " ".join(filter(None, [action.name, action.slug, action.description or ""]))
            )
            overlap = len(set(tokens) & set(action_tokens))
            bonus = 1 if any(token in action.slug.lower() for token in tokens) else 0
            score = overlap + bonus
            if best is None or score > best[1]:
                best = (action, score)

        return best

    def _extract_required_fields(self, action: TenantAction) -> list[str]:
        discovered: list[str] = []
        for source in (
            action.url_template or "",
            *(str(value) for value in (action.query_template or {}).values()),
            *(str(value) for value in (action.body_template or {}).values()),
        ):
            for match in PLACEHOLDER_PATTERN.findall(source):
                if match not in discovered:
                    discovered.append(match)
        return [field for field in discovered if self._looks_customer_collectible(field)]

    def _extract_current_inputs(
        self, required_fields: list[str], user_message: str
    ) -> dict[str, str]:
        inputs: dict[str, str] = {}
        for field in required_fields:
            value = self._extract_embedded_field_value(field, user_message)
            if value is not None:
                inputs[field] = value
        return inputs

    def _extract_field_value(self, field_name: str, user_message: str) -> str | None:
        cleaned = user_message.strip()
        if not cleaned:
            return None
        if "email" in field_name.lower():
            lowered = cleaned.lower()
            return lowered if EMAIL_PATTERN.fullmatch(lowered) else None
        return cleaned if IDENTIFIER_PATTERN.fullmatch(cleaned) else None

    def _extract_embedded_field_value(self, field_name: str, user_message: str) -> str | None:
        lowered = field_name.lower()
        stripped = user_message.strip()
        if not stripped:
            return None
        if "email" in lowered:
            match = EMAIL_PATTERN.search(stripped.lower())
            return match.group(0) if match else None
        if any(token in lowered for token in ("order", "account", "reference", "id")):
            direct = stripped.lstrip("#")
            if re.fullmatch(r"[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", direct):
                return direct
            token_match = re.search(r"#?([A-Za-z0-9_-]*\d[A-Za-z0-9_-]*)", stripped)
            if token_match:
                return token_match.group(1)
        return None

    def _build_lookup_plan(
        self,
        *,
        tenant_id: UUID,
        target_action: TenantAction,
        actions: list[TenantAction],
        target_missing_fields: list[str],
    ) -> LookupPlan | None:
        if not self._can_use_lookup_chains(tenant_id):
            return None

        explicit_plan = self._build_explicit_lookup_plan(
            target_action=target_action,
            actions=actions,
            target_missing_fields=target_missing_fields,
        )
        if explicit_plan:
            return explicit_plan

        target_field = next(
            (
                field
                for field in target_missing_fields
                if field.lower().endswith("_id") or field.lower().endswith("_reference")
            ),
            None,
        )
        if not target_field:
            return None

        target_domain = target_field.rsplit("_", 1)[0].lower()
        for action in actions:
            if action.id == target_action.id:
                continue
            lookup_name = " ".join(
                filter(None, [action.name, action.slug, action.description or ""])
            ).lower()
            if "lookup" not in lookup_name and "find" not in lookup_name:
                continue
            if target_domain not in lookup_name:
                continue
            lookup_required_fields = self._extract_required_fields(action)
            if not lookup_required_fields:
                continue
            if target_field in lookup_required_fields:
                continue
            return LookupPlan(
                lookup_action_id=str(action.id),
                target_field=target_field,
                lookup_required_fields=lookup_required_fields,
                remaining_target_fields=[
                    field for field in target_missing_fields if field != target_field
                ],
            )
        return None

    def _build_explicit_lookup_plan(
        self,
        *,
        target_action: TenantAction,
        actions: list[TenantAction],
        target_missing_fields: list[str],
    ) -> LookupPlan | None:
        input_schema = target_action.input_schema or {}
        if not isinstance(input_schema, dict):
            return None

        lookup_dependencies = input_schema.get("lookup_dependencies")
        if not isinstance(lookup_dependencies, dict):
            return None

        actions_by_slug = {action.slug: action for action in actions}
        for target_field in target_missing_fields:
            dependency = lookup_dependencies.get(target_field)
            if not isinstance(dependency, dict):
                continue

            lookup_slug = dependency.get("lookup_action_slug")
            if not isinstance(lookup_slug, str) or not lookup_slug.strip():
                continue

            lookup_action = actions_by_slug.get(lookup_slug.strip())
            if not lookup_action or lookup_action.id == target_action.id:
                continue

            lookup_required_fields = dependency.get("lookup_required_fields")
            if not isinstance(lookup_required_fields, list):
                lookup_required_fields = self._extract_required_fields(lookup_action)

            normalized_fields = [
                str(field)
                for field in lookup_required_fields
                if isinstance(field, str) and field.strip()
            ]
            if not normalized_fields:
                continue

            return LookupPlan(
                lookup_action_id=str(lookup_action.id),
                target_field=target_field,
                lookup_required_fields=normalized_fields,
                remaining_target_fields=[
                    field for field in target_missing_fields if field != target_field
                ],
            )

        return None

    async def _resolve_lookup_target_value(
        self,
        *,
        tenant_id: UUID,
        session_id: UUID,
        lookup_action: TenantAction,
        target_field: str,
        input_payload: dict[str, Any],
    ) -> str | None:
        execution = await self.automation_service.execute_action(
            tenant_id=tenant_id,
            action=lookup_action,
            input_payload=input_payload,
            session_id=session_id,
            trigger_source=ActionTriggerSource.AI_CHAT,
        )
        if execution.status != ActionExecutionStatus.SUCCEEDED:
            return None
        source = execution.normalized_output or execution.response_body or {}
        return self._find_field_in_payload(source, target_field)

    def _find_field_in_payload(self, payload: Any, target_field: str) -> str | None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if str(key).lower() == target_field.lower() and isinstance(
                    value, str | int | float
                ):
                    return str(value)
                nested = self._find_field_in_payload(value, target_field)
                if nested is not None:
                    return nested
        elif isinstance(payload, list):
            for item in payload:
                nested = self._find_field_in_payload(item, target_field)
                if nested is not None:
                    return nested
        return None

    async def _format_execution_response(
        self,
        *,
        action: TenantAction,
        execution,
        tenant_ai_config: Any | None,
        company_name: str | None,
        input_payload: dict[str, Any],
    ) -> tuple[str, bool, str | None]:
        if execution.status == ActionExecutionStatus.SUCCEEDED:
            llm_text = await self._generate_llm_execution_reply(
                action=action,
                execution=execution,
                tenant_ai_config=tenant_ai_config,
                company_name=company_name,
                input_payload=input_payload,
            )
            if llm_text:
                return (llm_text, False, None)
            summary = self._summarize_data(execution.normalized_output or execution.response_body)
            if summary:
                return (
                    f"I completed {action.name.lower()} successfully.\n\n{summary}",
                    False,
                    None,
                )
            return (f"I completed {action.name.lower()} successfully.", False, None)

        if execution.status == ActionExecutionStatus.BLOCKED_POLICY:
            if execution.error_code == "ACTION_APPROVAL_REQUIRED":
                return (
                    "This request needs approval from the support team before I can complete it. "
                    "Would you like me to connect you with someone from the team?",
                    True,
                    f"Automation action '{action.name}' requires approval",
                )
            if execution.error_code == "AUTOMATION_EXECUTION_LIMIT_REACHED":
                return (
                    "I could not complete that request because this business has reached its monthly automation limit.",
                    False,
                    None,
                )
            return (
                "I could not complete that request because this automation is currently blocked by policy.",
                False,
                None,
            )

        if execution.status == ActionExecutionStatus.TIMED_OUT:
            return (
                "I could not complete that request because the connected service took too long to respond.",
                False,
                None,
            )

        if execution.status == ActionExecutionStatus.FAILED:
            llm_failure_text = await self._generate_llm_failure_reply(
                action=action,
                execution=execution,
                tenant_ai_config=tenant_ai_config,
                company_name=company_name,
                input_payload=input_payload,
            )
            if llm_failure_text:
                return (llm_failure_text, False, None)

        return (
            "I could not complete that request from the connected service just now.",
            False,
            None,
        )

    async def _generate_llm_execution_reply(
        self,
        *,
        action: TenantAction,
        execution,
        tenant_ai_config: Any | None,
        company_name: str | None,
        input_payload: dict[str, Any],
    ) -> str | None:
        if tenant_ai_config is None:
            return None

        result_payload = execution.normalized_output or execution.response_body or {}
        prompt_text = (
            "You are writing the final customer-facing support reply for an automation action.\n"
            f"Tenant company: {company_name or 'the company'}\n"
            f"Action name: {action.name}\n"
            f"User-provided inputs: {input_payload}\n"
            f"Execution result: {result_payload}\n"
            "Instructions:\n"
            "- Respond naturally and professionally in chat format.\n"
            "- Use the execution result only. Do not invent facts.\n"
            "- Keep it concise and helpful.\n"
            "- If there is a status, explain it plainly.\n"
            "- Use a short conversational support reply, not an email or letter.\n"
            "- Do not include headings like Subject, Dear, Best regards, or sign-offs.\n"
            "- Do not mention internal system names like automation, API, normalized output, or action execution.\n"
            "- Do not use emojis.\n"
        )
        custom_instructions = getattr(tenant_ai_config, "custom_instructions", None)
        if custom_instructions:
            prompt_text += (
                f"- Follow these tenant instructions if relevant: {custom_instructions}\n"
            )

        rag_service = RAGService(self.db)
        prompt = SimpleNamespace(full_prompt=prompt_text, prompt_tokens=0)
        try:
            response_text, _, is_api_error = await asyncio.wait_for(
                rag_service.generate_ai_response(
                    prompt=prompt,
                    max_tokens=min(
                        getattr(tenant_ai_config, "max_response_tokens", 220), 220
                    ),
                ),
                timeout=self.LLM_REPLY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None
        if is_api_error:
            return None
        return response_text.strip() or None

    def _summarize_data(self, payload: Any) -> str:
        if not payload:
            return ""

        data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if isinstance(data, dict):
            lines: list[str] = []
            for key, value in list(data.items())[:6]:
                if isinstance(value, str | int | float | bool) and value not in ("", None):
                    lines.append(f"- {self._humanize_label(str(key))}: {value}")
            return "\n".join(lines)
        if isinstance(data, list) and data:
            return f"- Result count: {len(data)}"
        if isinstance(data, str | int | float | bool):
            return f"- Result: {data}"
        return ""

    def _build_missing_field_prompt(self, action_name: str, field_name: str) -> str:
        prompt = self._field_prompt_label(field_name)
        return f"To continue with {action_name.lower()}, please share {prompt}."

    def _build_invalid_field_prompt(self, field_name: str) -> str:
        prompt = self._field_prompt_label(field_name)
        if "email" in field_name.lower():
            return f"Please send a valid {prompt} so I can continue."
        return f"Please send {prompt} so I can continue."

    def _build_lookup_failure_prompt(self, field_name: str) -> str:
        label = self._humanize_label(field_name).lower()
        return f"I could not find a matching {label} from the details provided. Please check the information and try again."

    async def _generate_llm_failure_reply(
        self,
        *,
        action: TenantAction,
        execution,
        tenant_ai_config: Any | None,
        company_name: str | None,
        input_payload: dict[str, Any],
    ) -> str | None:
        fallback = self._fallback_failure_reply(action=action, execution=execution)
        if tenant_ai_config is None:
            return fallback

        prompt_text = (
            "You are writing a short customer-facing support reply after an attempted action failed.\n"
            f"Tenant company: {company_name or 'the company'}\n"
            f"Action name: {action.name}\n"
            f"User-provided inputs: {input_payload}\n"
            f"Provider HTTP status: {execution.response_status_code}\n"
            f"Provider response body: {execution.response_body}\n"
            f"Internal error message: {execution.error_message}\n"
            "Instructions:\n"
            "- Explain the failure clearly and naturally in chat format.\n"
            "- If the failure indicates the record was not found, tell the customer that you could not find a matching record and ask them to check the detail they shared.\n"
            "- If the failure is different, give a short, professional explanation without technical jargon.\n"
            "- Do not write an email or letter.\n"
            "- Do not mention internal system names like API, provider, payload, or execution.\n"
            "- Do not invent any facts beyond the failure details given.\n"
            "- Do not use emojis.\n"
        )
        generated = await self._generate_short_llm_text(
            prompt_text=prompt_text,
            tenant_ai_config=tenant_ai_config,
            max_tokens=140,
        )
        return generated or fallback

    def _fallback_failure_reply(self, *, action: TenantAction, execution) -> str:
        response_status = getattr(execution, "response_status_code", None)
        response_body = getattr(execution, "response_body", None) or {}
        error_message = (getattr(execution, "error_message", None) or "").strip()

        if response_status == 404:
            resource_label = self._resource_label_from_action(action)
            return (
                f"I could not find a matching {resource_label} with the details provided. "
                "Please check the information and try again."
            )

        detail = None
        if isinstance(response_body, dict):
            raw_detail = response_body.get("detail")
            if isinstance(raw_detail, str) and raw_detail.strip():
                detail = raw_detail.strip()

        if detail:
            return f"I could not complete that request because {detail.lower()}."
        if error_message:
            return "I could not complete that request from the connected service right now."
        return "I could not complete that request from the connected service just now."

    async def _generate_missing_field_prompt(
        self,
        *,
        action: TenantAction,
        field_name: str,
        tenant_ai_config: Any | None,
        company_name: str | None,
        fallback_action_name: str,
    ) -> str:
        fallback = self._build_missing_field_prompt(fallback_action_name, field_name)
        if tenant_ai_config is None:
            return fallback

        prompt_text = (
            "You are writing a short customer-facing follow-up question for a support assistant.\n"
            f"Tenant company: {company_name or 'the company'}\n"
            f"Action name: {action.name}\n"
            f"Action description: {action.description or ''}\n"
            f"Missing customer detail: {field_name}\n"
            f"Helpful field explanation: {self._field_prompt_label(field_name)}\n"
            "Instructions:\n"
            "- Ask for only the one missing detail needed right now.\n"
            "- Keep it natural, concise, and professional.\n"
            "- Write it as a chat message, not an email or formal letter.\n"
            "- Do not mention internal terms like action, automation, field, payload, or variable.\n"
            "- Do not use emojis.\n"
            "- Do not sound generic if the company or action context suggests a more natural phrasing.\n"
        )
        generated = await self._generate_short_llm_text(
            prompt_text=prompt_text,
            tenant_ai_config=tenant_ai_config,
        )
        return generated or fallback

    async def _generate_invalid_field_prompt(
        self,
        *,
        action: TenantAction,
        field_name: str,
        tenant_ai_config: Any | None,
        company_name: str | None,
    ) -> str:
        fallback = self._build_invalid_field_prompt(field_name)
        if tenant_ai_config is None:
            return fallback

        prompt_text = (
            "You are writing a short customer-facing correction prompt for a support assistant.\n"
            f"Tenant company: {company_name or 'the company'}\n"
            f"Action name: {action.name}\n"
            f"Invalid or missing customer detail: {field_name}\n"
            f"Helpful field explanation: {self._field_prompt_label(field_name)}\n"
            "Instructions:\n"
            "- Briefly explain that the last detail was not usable.\n"
            "- Ask for the same detail again in a clear, natural way.\n"
            "- Keep it concise and professional.\n"
            "- Write it as a chat message, not an email or formal letter.\n"
            "- Do not mention internal terms like variable, field, validation, or payload.\n"
            "- Do not use emojis.\n"
        )
        generated = await self._generate_short_llm_text(
            prompt_text=prompt_text,
            tenant_ai_config=tenant_ai_config,
        )
        return generated or fallback

    async def _generate_short_llm_text(
        self,
        *,
        prompt_text: str,
        tenant_ai_config: Any,
        max_tokens: int = 120,
    ) -> str | None:
        custom_instructions = getattr(tenant_ai_config, "custom_instructions", None)
        if custom_instructions:
            prompt_text += (
                f"- Follow these tenant instructions if relevant: {custom_instructions}\n"
            )

        rag_service = RAGService(self.db)
        prompt = SimpleNamespace(full_prompt=prompt_text, prompt_tokens=0)
        try:
            response_text, _, is_api_error = await asyncio.wait_for(
                rag_service.generate_ai_response(
                    prompt=prompt,
                    max_tokens=min(
                        getattr(tenant_ai_config, "max_response_tokens", max_tokens),
                        max_tokens,
                    ),
                ),
                timeout=self.LLM_REPLY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None
        if is_api_error:
            return None
        cleaned = response_text.strip()
        return cleaned or None

    def _resource_label_from_action(self, action: TenantAction) -> str:
        label = "record"
        action_text = " ".join(
            filter(None, [action.name, action.slug, action.description or ""])
        ).lower()
        if "order" in action_text:
            label = "order"
        elif "account" in action_text:
            label = "account"
        elif "ticket" in action_text:
            label = "ticket"
        elif "appointment" in action_text or "booking" in action_text:
            label = "appointment"
        return label

    def _field_prompt_label(self, field_name: str) -> str:
        lowered = field_name.lower()
        if "email" in lowered:
            return "the email address to use"
        if "order" in lowered:
            return "the order number or order ID"
        if "account" in lowered:
            return "the account ID or account reference"
        if "ticket" in lowered and "subject" in lowered:
            return "a short issue title"
        if "ticket" in lowered or "message" in lowered or "reason" in lowered:
            return "the details for this request"
        if "date" in lowered:
            return "the preferred date"
        if "time" in lowered:
            return "the preferred time"
        return f"the {self._humanize_label(field_name).lower()}"

    def _humanize_label(self, raw: str) -> str:
        return raw.replace("_", " ").replace(".", " ").strip().title()

    def _looks_customer_collectible(self, field_name: str) -> bool:
        lowered = field_name.lower()
        if lowered.startswith("secret_") or "token" in lowered or "key" in lowered:
            return False
        return any(hint in lowered for hint in CUSTOMER_FIELD_HINTS)

    def _tokenize(self, text: str) -> list[str]:
        normalized = re.sub(r"[^a-z0-9]+", " ", text.lower())
        return [token for token in normalized.split() if token and token not in STOPWORDS]
