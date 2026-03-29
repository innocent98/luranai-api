"""Service for tenant automation actions and execution audit."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.automation import (
    ActionExecution,
    ActionExecutionStatus,
    ActionSecret,
    ActionTriggerSource,
    TenantAction,
    TenantActionStatus,
)
from app.db.models.tenant import TenantCapabilities
from app.schemas.automation import TenantActionCreate, TenantActionUpdate
from app.utils.network_security import ensure_safe_outbound_url


class AutomationService:
    """Service for CRUD and runtime execution of tenant actions."""

    EXECUTION_LIMIT_ERROR_CODE = "AUTOMATION_EXECUTION_LIMIT_REACHED"

    def __init__(self, db: Session):
        self.db = db

    def create_action(
        self, tenant_id: UUID, user_id: UUID | None, payload: TenantActionCreate
    ) -> TenantAction:
        self._check_action_limit(tenant_id)
        data = payload.model_dump()
        if data.get("is_sensitive") and not data.get("requires_approval"):
            data["requires_approval"] = True

        action = TenantAction(
            tenant_id=tenant_id,
            created_by=user_id,
            updated_by=user_id,
            **data,
        )
        self.db.add(action)
        self.db.commit()
        self.db.refresh(action)
        return action

    def count_actions(self, tenant_id: UUID) -> int:
        stmt = select(func.count()).select_from(TenantAction).where(TenantAction.tenant_id == tenant_id)
        return int(self.db.execute(stmt).scalar() or 0)

    def _check_action_limit(self, tenant_id: UUID) -> None:
        stmt = select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        capabilities = self.db.execute(stmt).scalar_one_or_none()
        if not capabilities or capabilities.max_automation_actions is None:
            return

        action_count = self.count_actions(tenant_id)
        if action_count >= capabilities.max_automation_actions:
            raise ValueError(
                f"Automation action limit reached. Your plan allows {capabilities.max_automation_actions} automation actions. Upgrade to create more."
            )

    def count_monthly_executions(self, tenant_id: UUID) -> int:
        month_start = datetime.now(timezone.utc).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        stmt = (
            select(func.count())
            .select_from(ActionExecution)
            .where(
                ActionExecution.tenant_id == tenant_id,
                ActionExecution.created_at >= month_start,
                or_(
                    ActionExecution.error_code.is_(None),
                    ActionExecution.error_code != self.EXECUTION_LIMIT_ERROR_CODE,
                ),
            )
        )
        return int(self.db.execute(stmt).scalar() or 0)

    def _get_execution_limit(self, tenant_id: UUID) -> int | None:
        stmt = select(TenantCapabilities).where(TenantCapabilities.tenant_id == tenant_id)
        capabilities = self.db.execute(stmt).scalar_one_or_none()
        if not capabilities:
            return None
        return capabilities.max_automation_executions_per_month

    def _build_execution_limit_blocked_execution(
        self,
        *,
        tenant_id: UUID,
        action: TenantAction,
        input_payload: dict[str, Any],
        session_id: UUID | None,
        flow_execution_id: UUID | None,
        flow_id: UUID | None,
        node_id: str | None,
        trigger_source: ActionTriggerSource,
        monthly_limit: int,
    ) -> ActionExecution:
        execution = ActionExecution(
            tenant_id=tenant_id,
            session_id=session_id,
            flow_execution_id=flow_execution_id,
            flow_id=flow_id,
            node_id=node_id,
            action_id=action.id,
            trigger_source=trigger_source,
            status=ActionExecutionStatus.BLOCKED_POLICY,
            input_payload=input_payload,
            resolved_request={},
            attempt=1,
            requires_approval=bool(action.requires_approval),
            approval_status="pending" if action.requires_approval else None,
            error_code=self.EXECUTION_LIMIT_ERROR_CODE,
            error_message=(
                f"Monthly automation execution limit reached. Your plan allows "
                f"{monthly_limit} automation executions per month. Upgrade to run more."
            ),
            meta={"blocked_by": "automation_execution_limit"},
        )
        self.db.add(execution)
        self.db.commit()
        self.db.refresh(execution)
        return execution

    def create_secret(
        self, tenant_id: UUID, provider: str, key_name: str, secret_value: str
    ) -> ActionSecret:
        existing = self.get_secret_by_key(tenant_id=tenant_id, provider=provider, key_name=key_name)
        if existing:
            existing.secret_value = secret_value
            existing.status = "active"
            self.db.commit()
            self.db.refresh(existing)
            return existing

        secret = ActionSecret(
            tenant_id=tenant_id,
            provider=provider,
            key_name=key_name,
            secret_value=secret_value,
            status="active",
        )
        self.db.add(secret)
        self.db.commit()
        self.db.refresh(secret)
        return secret

    def get_action_by_slug(self, tenant_id: UUID, slug: str) -> TenantAction | None:
        stmt = select(TenantAction).where(
            TenantAction.tenant_id == tenant_id,
            TenantAction.slug == slug,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_secrets(self, tenant_id: UUID) -> list[ActionSecret]:
        stmt = (
            select(ActionSecret)
            .where(ActionSecret.tenant_id == tenant_id)
            .order_by(ActionSecret.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_secret(self, tenant_id: UUID, secret_id: UUID) -> ActionSecret | None:
        stmt = select(ActionSecret).where(
            ActionSecret.id == secret_id,
            ActionSecret.tenant_id == tenant_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_secret_by_key(
        self, tenant_id: UUID, provider: str, key_name: str
    ) -> ActionSecret | None:
        stmt = select(ActionSecret).where(
            ActionSecret.tenant_id == tenant_id,
            ActionSecret.provider == provider,
            ActionSecret.key_name == key_name,
            ActionSecret.status == "active",
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def delete_secret(self, tenant_id: UUID, secret_id: UUID) -> bool:
        secret = self.get_secret(tenant_id=tenant_id, secret_id=secret_id)
        if not secret:
            return False
        self.db.delete(secret)
        self.db.commit()
        return True

    def list_actions(self, tenant_id: UUID) -> list[TenantAction]:
        stmt = (
            select(TenantAction)
            .where(TenantAction.tenant_id == tenant_id)
            .order_by(TenantAction.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_action(self, tenant_id: UUID, action_id: UUID) -> TenantAction | None:
        stmt = select(TenantAction).where(
            TenantAction.id == action_id,
            TenantAction.tenant_id == tenant_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def update_action(
        self, tenant_id: UUID, action_id: UUID, user_id: UUID | None, payload: TenantActionUpdate
    ) -> TenantAction | None:
        action = self.get_action(tenant_id=tenant_id, action_id=action_id)
        if not action:
            return None
        patch = payload.model_dump(exclude_unset=True)
        if patch.get("is_sensitive") is True and "requires_approval" not in patch:
            patch["requires_approval"] = True
        for field, value in patch.items():
            setattr(action, field, value)
        action.updated_by = user_id
        self.db.commit()
        self.db.refresh(action)
        return action

    def deactivate_action(self, tenant_id: UUID, action_id: UUID) -> TenantAction | None:
        action = self.get_action(tenant_id=tenant_id, action_id=action_id)
        if not action:
            return None
        action.status = TenantActionStatus.INACTIVE
        self.db.commit()
        self.db.refresh(action)
        return action

    def delete_action(self, tenant_id: UUID, action_id: UUID) -> bool:
        action = self.get_action(tenant_id=tenant_id, action_id=action_id)
        if not action:
            return False
        self.db.delete(action)
        self.db.commit()
        return True

    def list_executions(
        self, tenant_id: UUID, action_id: UUID | None = None, limit: int = 100
    ) -> list[ActionExecution]:
        stmt = select(ActionExecution).where(ActionExecution.tenant_id == tenant_id)
        if action_id:
            stmt = stmt.where(ActionExecution.action_id == action_id)
        stmt = stmt.order_by(ActionExecution.created_at.desc()).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def get_execution(self, tenant_id: UUID, execution_id: UUID) -> ActionExecution | None:
        stmt = select(ActionExecution).where(
            ActionExecution.id == execution_id,
            ActionExecution.tenant_id == tenant_id,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    async def retry_execution(self, tenant_id: UUID, execution_id: UUID) -> ActionExecution | None:
        execution = self.get_execution(tenant_id=tenant_id, execution_id=execution_id)
        if not execution:
            return None

        action = self.get_action(tenant_id=tenant_id, action_id=execution.action_id)
        if not action:
            execution.status = ActionExecutionStatus.FAILED
            execution.error_code = "ACTION_NOT_FOUND"
            execution.error_message = "Action not found for retry."
            self.db.commit()
            self.db.refresh(execution)
            return execution

        retried = await self.execute_action(
            tenant_id=tenant_id,
            action=action,
            input_payload=execution.input_payload or {},
            session_id=execution.session_id,
            flow_execution_id=execution.flow_execution_id,
            flow_id=execution.flow_id,
            node_id=execution.node_id,
            trigger_source=execution.trigger_source,
        )
        retried.meta = {
            **(retried.meta or {}),
            "retry_of_execution_id": str(execution_id),
        }
        self.db.commit()
        self.db.refresh(retried)
        return retried

    async def set_execution_approval(
        self, tenant_id: UUID, execution_id: UUID, approved: bool, approved_by: UUID | None
    ) -> ActionExecution | None:
        execution = self.get_execution(tenant_id=tenant_id, execution_id=execution_id)
        if not execution:
            return None

        execution.approval_status = "approved" if approved else "rejected"
        execution.approved_by = approved_by
        execution.meta = {
            **(execution.meta or {}),
            "approval_decision": execution.approval_status,
        }
        self.db.commit()
        self.db.refresh(execution)

        if not approved:
            return execution

        action = self.get_action(tenant_id=tenant_id, action_id=execution.action_id)
        if not action:
            execution.error_code = "ACTION_NOT_FOUND"
            execution.error_message = "Action not found after approval."
            execution.status = ActionExecutionStatus.FAILED
            self.db.commit()
            self.db.refresh(execution)
            return execution

        rerun = await self.execute_action(
            tenant_id=tenant_id,
            action=action,
            input_payload=execution.input_payload or {},
            session_id=execution.session_id,
            flow_execution_id=execution.flow_execution_id,
            flow_id=execution.flow_id,
            node_id=execution.node_id,
            trigger_source=execution.trigger_source,
            bypass_approval_check=True,
        )
        rerun.meta = {
            **(rerun.meta or {}),
            "approved_from_execution_id": str(execution_id),
        }
        self.db.commit()
        self.db.refresh(rerun)
        return rerun

    async def execute_action(
        self,
        *,
        tenant_id: UUID,
        action: TenantAction,
        input_payload: dict[str, Any] | None = None,
        session_id: UUID | None = None,
        flow_execution_id: UUID | None = None,
        flow_id: UUID | None = None,
        node_id: str | None = None,
        trigger_source: ActionTriggerSource = ActionTriggerSource.FLOW_NODE,
        bypass_approval_check: bool = False,
    ) -> ActionExecution:
        input_payload = input_payload or {}
        monthly_execution_limit = self._get_execution_limit(tenant_id)
        if monthly_execution_limit is not None:
            monthly_execution_count = self.count_monthly_executions(tenant_id)
            if monthly_execution_count >= monthly_execution_limit:
                return self._build_execution_limit_blocked_execution(
                    tenant_id=tenant_id,
                    action=action,
                    input_payload=input_payload,
                    session_id=session_id,
                    flow_execution_id=flow_execution_id,
                    flow_id=flow_id,
                    node_id=node_id,
                    trigger_source=trigger_source,
                    monthly_limit=monthly_execution_limit,
                )

        execution = ActionExecution(
            tenant_id=tenant_id,
            session_id=session_id,
            flow_execution_id=flow_execution_id,
            flow_id=flow_id,
            node_id=node_id,
            action_id=action.id,
            trigger_source=trigger_source,
            status=ActionExecutionStatus.QUEUED,
            input_payload=input_payload,
            resolved_request={},
            attempt=1,
            requires_approval=bool(action.requires_approval),
            approval_status="pending" if action.requires_approval else None,
            meta={},
        )
        self.db.add(execution)
        self.db.commit()
        self.db.refresh(execution)

        if not action.allow_in_flows and trigger_source == ActionTriggerSource.FLOW_NODE:
            execution.status = ActionExecutionStatus.BLOCKED_POLICY
            execution.error_code = "ACTION_NOT_ALLOWED_IN_FLOWS"
            execution.error_message = "Action is not allowed in flow runtime."
            self.db.commit()
            self.db.refresh(execution)
            return execution

        if action.status != TenantActionStatus.ACTIVE:
            execution.status = ActionExecutionStatus.BLOCKED_POLICY
            execution.error_code = "ACTION_DISABLED"
            execution.error_message = "Action is not active."
            self.db.commit()
            self.db.refresh(execution)
            return execution

        if action.requires_approval and not bypass_approval_check:
            execution.status = ActionExecutionStatus.BLOCKED_POLICY
            execution.error_code = "ACTION_APPROVAL_REQUIRED"
            execution.error_message = "Action requires approval before execution."
            self.db.commit()
            self.db.refresh(execution)
            return execution

        url = self._interpolate_variables(action.url_template, input_payload)
        headers = self._interpolate_dict(action.headers_template, input_payload)
        query = self._interpolate_dict(action.query_template, input_payload)
        body = self._interpolate_dict(action.body_template, input_payload)
        method = action.method.upper()

        try:
            ensure_safe_outbound_url(
                url,
                allowed_hostnames=settings.get_automation_outbound_host_allowlist(),
            )
        except ValueError as e:
            execution.status = ActionExecutionStatus.BLOCKED_POLICY
            execution.error_code = "UNSAFE_URL"
            execution.error_message = str(e)
            execution.resolved_request = {
                "method": method,
                "url": url,
                "headers": self._redact_headers(headers),
                "query": query,
            }
            self.db.commit()
            self.db.refresh(execution)
            return execution

        auth_config_with_context = {
            **(action.auth_config or {}),
            "__tenant_id": str(tenant_id),
        }
        headers = self._apply_auth_headers(
            headers, action.auth_type.value, auth_config_with_context
        )
        execution.status = ActionExecutionStatus.RUNNING
        execution.resolved_request = {
            "method": method,
            "url": url,
            "headers": self._redact_headers(headers),
            "query": query,
            "body": body,
        }
        self.db.commit()

        started = perf_counter()
        try:
            async with httpx.AsyncClient(timeout=action.timeout_seconds) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=query if query else None,
                    json=body if method in {"POST", "PUT", "PATCH", "DELETE"} else None,
                )
            latency_ms = int((perf_counter() - started) * 1000)
            parsed_body = self._parse_response_body(response)

            execution.response_status_code = response.status_code
            execution.response_headers = dict(response.headers)
            execution.response_body = parsed_body
            execution.latency_ms = latency_ms

            if 200 <= response.status_code < 300:
                execution.status = ActionExecutionStatus.SUCCEEDED
                execution.normalized_output = {
                    "status_code": response.status_code,
                    "success": True,
                    "data": parsed_body,
                }
            else:
                execution.status = ActionExecutionStatus.FAILED
                execution.error_code = "PROVIDER_ERROR"
                execution.error_message = f"Provider returned HTTP {response.status_code}"
                execution.normalized_output = {
                    "status_code": response.status_code,
                    "success": False,
                    "data": parsed_body,
                }
            self.db.commit()
            self.db.refresh(execution)
            return execution
        except httpx.TimeoutException:
            execution.status = ActionExecutionStatus.TIMED_OUT
            execution.error_code = "ACTION_TIMEOUT"
            execution.error_message = f"Action timed out after {action.timeout_seconds}s"
            execution.latency_ms = int((perf_counter() - started) * 1000)
            self.db.commit()
            self.db.refresh(execution)
            return execution
        except Exception as e:
            logger.error(f"Action execution failed ({action.id}): {e}", exc_info=True)
            execution.status = ActionExecutionStatus.FAILED
            execution.error_code = "ACTION_EXECUTION_ERROR"
            execution.error_message = str(e)
            execution.latency_ms = int((perf_counter() - started) * 1000)
            self.db.commit()
            self.db.refresh(execution)
            return execution

    def _apply_auth_headers(self, headers: dict, auth_type: str, auth_config: dict) -> dict:
        result = dict(headers or {})
        tenant_id_raw = auth_config.get("__tenant_id")
        tenant_id = UUID(str(tenant_id_raw)) if tenant_id_raw else None
        if auth_type == "bearer":
            token = auth_config.get("bearer_token") or auth_config.get("token")
            if not token and auth_config.get("secret_key_name") and tenant_id:
                provider = str(auth_config.get("provider", "default"))
                key_name = str(auth_config["secret_key_name"])
                secret = self.get_secret_by_key(
                    tenant_id=tenant_id,
                    provider=provider,
                    key_name=key_name,
                )
                token = secret.secret_value if secret else None
            if token:
                result["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key_header":
            key_name = auth_config.get("header_name", "X-API-Key")
            key_value = auth_config.get("api_key")
            if not key_value and auth_config.get("secret_key_name") and tenant_id:
                provider = str(auth_config.get("provider", "default"))
                secret_key_name = str(auth_config["secret_key_name"])
                secret = self.get_secret_by_key(
                    tenant_id=tenant_id,
                    provider=provider,
                    key_name=secret_key_name,
                )
                key_value = secret.secret_value if secret else None
            if key_value:
                result[str(key_name)] = str(key_value)
        return result

    def _parse_response_body(self, response: httpx.Response) -> Any:
        if not response.text:
            return None
        try:
            return response.json()
        except Exception:
            return response.text

    def _interpolate_variables(self, template: str, variables: dict) -> str:
        if not template:
            return ""
        pattern = r"\{\{([^}]+)\}\}"

        def replacer(match: re.Match) -> str:
            var_name = match.group(1).strip()
            value: Any = variables
            for key in var_name.split("."):
                if isinstance(value, dict):
                    value = value.get(key, f"{{{{{var_name}}}}}")
                else:
                    return f"{{{{{var_name}}}}}"
            return str(value) if value is not None else f"{{{{{var_name}}}}}"

        return re.sub(pattern, replacer, template)

    def _interpolate_dict(self, data: dict, variables: dict) -> dict:
        result: dict[str, Any] = {}
        for key, value in (data or {}).items():
            if isinstance(value, str):
                result[key] = self._interpolate_variables(value, variables)
            elif isinstance(value, dict):
                result[key] = self._interpolate_dict(value, variables)
            elif isinstance(value, list):
                result[key] = [
                    self._interpolate_variables(v, variables) if isinstance(v, str) else v
                    for v in value
                ]
            else:
                result[key] = value
        return result

    def _redact_headers(self, headers: dict) -> dict:
        redacted = {}
        for key, value in (headers or {}).items():
            if key.lower() in {"authorization", "x-api-key", "api-key"}:
                redacted[key] = "***"
            else:
                redacted[key] = value
        return redacted
