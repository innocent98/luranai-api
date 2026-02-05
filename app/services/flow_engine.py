"""Flow engine service for executing conversational workflows."""

import re
import httpx
from datetime import datetime
from typing import Optional, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select
from loguru import logger

from app.db.models.flow import Flow, FlowExecution, FlowStatus, FlowExecutionStatus
from app.db.models.conversation import ConversationSession
from app.services.rag_service import RAGService


class FlowEngine:
    """
    Service for executing conversational flow automation.

    Handles flow matching, node execution, and state management for
    automated conversation workflows.
    """

    def __init__(self, db: Session):
        """Initialize flow engine with database session."""
        self.db = db
        self.rag_service = RAGService(db)

    def match_flow(
        self, message: str, session: ConversationSession
    ) -> Optional[Flow]:
        """
        Find matching flow based on triggers and message content.

        Args:
            message: User message content
            session: Conversation session

        Returns:
            Matching flow if found, None otherwise
        """
        # First, check if there's an active flow execution for this session
        active_exec = self._get_active_execution(session.id)
        if active_exec:
            logger.info(
                f"Continuing active flow: {active_exec.flow.name} (id={active_exec.flow_id})"
            )
            return active_exec.flow

        # Get all active flows for tenant, ordered by priority
        stmt = (
            select(Flow)
            .where(
                Flow.tenant_id == session.tenant_id,
                Flow.is_active == True,
                Flow.status == FlowStatus.ACTIVE,
            )
            .order_by(Flow.priority.desc())
        )

        flows = self.db.execute(stmt).scalars().all()

        # Try to match each flow in priority order
        for flow in flows:
            if self._matches_trigger(flow, message, session):
                logger.info(
                    f"Flow matched: {flow.name} (id={flow.id}) for session {session.id}"
                )
                return flow

        return None

    def _matches_trigger(
        self, flow: Flow, message: str, session: ConversationSession
    ) -> bool:
        """
        Check if flow trigger conditions are met.

        Args:
            flow: Flow to check
            message: User message content
            session: Conversation session

        Returns:
            True if trigger matches, False otherwise
        """
        from app.db.models.flow import FlowTriggerType

        trigger_type = flow.trigger_type
        trigger_config = flow.trigger_config or {}

        # KEYWORD trigger: match exact keywords or patterns
        if trigger_type == FlowTriggerType.KEYWORD:
            keywords = trigger_config.get("keywords", [])
            message_lower = message.lower()

            for keyword in keywords:
                keyword_lower = keyword.lower()
                # Support both exact match and word boundary match
                if keyword_lower in message_lower:
                    return True
                # Check for word boundary match (whole words only)
                pattern = r'\b' + re.escape(keyword_lower) + r'\b'
                if re.search(pattern, message_lower):
                    return True

            return False

        # INTENT trigger: semantic matching (future enhancement)
        elif trigger_type == FlowTriggerType.INTENT:
            # TODO: Implement intent-based matching using embeddings
            logger.debug("Intent-based triggers not yet implemented")
            return False

        # MANUAL trigger: only triggered explicitly via API
        elif trigger_type == FlowTriggerType.MANUAL:
            return False

        # SCHEDULE trigger: time-based (not message-triggered)
        elif trigger_type == FlowTriggerType.SCHEDULE:
            return False

        # EVENT trigger: system events (not message-triggered)
        elif trigger_type == FlowTriggerType.EVENT:
            return False

        return False

    def _get_active_execution(
        self, session_id: UUID
    ) -> Optional[FlowExecution]:
        """
        Get active flow execution for session.

        Args:
            session_id: Conversation session ID

        Returns:
            Active execution if found, None otherwise
        """
        stmt = (
            select(FlowExecution)
            .where(
                FlowExecution.session_id == session_id,
                FlowExecution.status.in_([
                    FlowExecutionStatus.PENDING,
                    FlowExecutionStatus.RUNNING,
                ]),
            )
            .order_by(FlowExecution.created_at.desc())
        )

        return self.db.execute(stmt).scalar_one_or_none()

    async def execute_node(
        self, execution: FlowExecution, user_input: Optional[str] = None
    ) -> dict:
        """
        Execute current node in flow execution and advance to next.

        Args:
            execution: Flow execution instance
            user_input: Optional user input for interactive nodes

        Returns:
            Flow execution result dict with:
                - message: Response message to show user
                - buttons: Optional button options
                - is_complete: Whether flow is finished
                - use_rag_response: Whether to use RAG for follow-up
                - should_escalate: Whether to escalate to human
        """
        # Get flow definition
        stmt = select(Flow).where(Flow.id == execution.flow_id)
        flow = self.db.execute(stmt).scalar_one_or_none()

        if not flow:
            return self._error_result("Flow not found")

        # Update execution status to running if pending
        if execution.status == FlowExecutionStatus.PENDING:
            execution.status = FlowExecutionStatus.RUNNING
            execution.started_at = datetime.utcnow()
            self.db.commit()

        # Get current node
        nodes_config = flow.nodes or {}
        nodes = nodes_config.get("nodes", [])
        current_node_id = execution.current_node_id or nodes_config.get("start_node_id")

        if not current_node_id:
            return self._error_result("No start node defined")

        current_node = next(
            (n for n in nodes if n.get("id") == current_node_id), None
        )

        if not current_node:
            return self._error_result(f"Node {current_node_id} not found")

        # Execute node based on type
        node_type = current_node.get("type")
        logger.debug(
            f"Executing node {current_node_id} of type {node_type} for execution {execution.id}"
        )

        try:
            if node_type == "message":
                result = await self._exec_message(execution, current_node)
            elif node_type == "question":
                result = await self._exec_question(execution, current_node, user_input)
            elif node_type == "text_input":
                result = await self._exec_text_input(execution, current_node, user_input)
            elif node_type == "condition":
                result = await self._exec_condition(execution, current_node)
            elif node_type == "rag_query":
                result = await self._exec_rag_query(execution, current_node)
            elif node_type == "webhook":
                result = await self._exec_webhook(execution, current_node)
            elif node_type == "escalate":
                result = await self._exec_escalate(execution, current_node)
            elif node_type == "end":
                result = await self._exec_end(execution, current_node)
            else:
                result = self._error_result(f"Unknown node type: {node_type}")

            # Save collected data
            self.db.commit()

            return result

        except Exception as e:
            logger.error(f"Error executing node {current_node_id}: {e}", exc_info=True)
            execution.status = FlowExecutionStatus.FAILED
            execution.error_message = str(e)
            execution.completed_at = datetime.utcnow()
            self.db.commit()

            return self._error_result(f"Flow execution failed: {str(e)}")

    async def _exec_message(
        self, execution: FlowExecution, node: dict
    ) -> dict:
        """
        Execute MESSAGE node: send message and auto-advance.

        Args:
            execution: Flow execution
            node: Node configuration

        Returns:
            Execution result
        """
        config = node.get("config", {})
        message_template = config.get("message", "")

        # Interpolate variables
        message = self._interpolate_variables(
            message_template, execution.collected_data
        )

        # Auto-advance to next node
        next_node_id = self._get_next_node(node, execution)
        execution.current_node_id = next_node_id
        self.db.commit()

        return {
            "message": message,
            "buttons": None,
            "is_complete": next_node_id is None,
            "use_rag_response": False,
            "should_escalate": False,
        }

    async def _exec_question(
        self, execution: FlowExecution, node: dict, user_input: Optional[str]
    ) -> dict:
        """
        Execute QUESTION node: present buttons and match response.

        Args:
            execution: Flow execution
            node: Node configuration
            user_input: User's response

        Returns:
            Execution result
        """
        config = node.get("config", {})
        question = config.get("question", "")
        options = config.get("options", [])

        # Interpolate variables in question
        question = self._interpolate_variables(question, execution.collected_data)

        # If no user input yet, present question
        if user_input is None:
            return {
                "message": question,
                "buttons": [opt.get("label") for opt in options],
                "is_complete": False,
                "use_rag_response": False,
                "should_escalate": False,
            }

        # Match user input to options
        user_input_lower = user_input.lower().strip()
        matched_option = None

        for option in options:
            label = option.get("label", "").lower()
            value = option.get("value", "").lower()

            if user_input_lower == label or user_input_lower == value:
                matched_option = option
                break

        if not matched_option:
            # Check for partial match
            for option in options:
                label = option.get("label", "").lower()
                if user_input_lower in label or label in user_input_lower:
                    matched_option = option
                    break

        if not matched_option:
            # Invalid response - ask again
            return {
                "message": f"I didn't quite get that. {question}",
                "buttons": [opt.get("label") for opt in options],
                "is_complete": False,
                "use_rag_response": False,
                "should_escalate": False,
            }

        # Save selected option
        save_to = config.get("save_to", "question_response")
        execution.collected_data[save_to] = matched_option.get("value")
        self.db.flush()

        # Advance to option's target node
        next_node_id = matched_option.get("next_node_id")
        execution.current_node_id = next_node_id
        self.db.commit()

        # Return acknowledgment
        acknowledgment = matched_option.get("acknowledgment", "Got it!")
        return {
            "message": acknowledgment,
            "buttons": None,
            "is_complete": next_node_id is None,
            "use_rag_response": False,
            "should_escalate": False,
        }

    async def _exec_text_input(
        self, execution: FlowExecution, node: dict, user_input: Optional[str]
    ) -> dict:
        """
        Execute TEXT_INPUT node: collect input with validation.

        Args:
            execution: Flow execution
            node: Node configuration
            user_input: User's input

        Returns:
            Execution result
        """
        config = node.get("config", {})
        prompt = config.get("prompt", "Please provide your input:")
        save_to = config.get("save_to", "text_input")
        validation = config.get("validation", {})

        # Interpolate prompt
        prompt = self._interpolate_variables(prompt, execution.collected_data)

        # If no user input yet, present prompt
        if user_input is None or user_input.strip() == "":
            return {
                "message": prompt,
                "buttons": None,
                "is_complete": False,
                "use_rag_response": False,
                "should_escalate": False,
            }

        # Validate input
        if validation:
            validation_result = self._validate_input(user_input, validation)
            if not validation_result.get("valid"):
                error_message = validation_result.get("error", "Invalid input")
                return {
                    "message": f"{error_message} {prompt}",
                    "buttons": None,
                    "is_complete": False,
                    "use_rag_response": False,
                    "should_escalate": False,
                }

        # Save input
        execution.collected_data[save_to] = user_input
        self.db.flush()

        # Advance to next node
        next_node_id = self._get_next_node(node, execution)
        execution.current_node_id = next_node_id
        self.db.commit()

        # Check if we should auto-continue to next node
        acknowledgment = config.get("acknowledgment", "Thank you!")

        if next_node_id:
            # Check if next node is non-interactive (should auto-execute)
            nodes = execution.flow.nodes.get("nodes", [])
            next_node = next((n for n in nodes if n.get("id") == next_node_id), None)
            if next_node:
                next_node_type = next_node.get("type")
                # Auto-continue for non-interactive nodes
                if next_node_type in ["webhook", "rag_query", "condition", "message", "end", "escalate"]:
                    # Send acknowledgment first, then continue execution
                    # Return with message but also continue execution flag
                    return {
                        "message": acknowledgment,
                        "buttons": None,
                        "is_complete": False,
                        "use_rag_response": False,
                        "should_escalate": False,
                        "continue_execution": True,  # Signal to continue
                    }

        # For interactive nodes or end of flow, just return acknowledgment
        return {
            "message": acknowledgment,
            "buttons": None,
            "is_complete": next_node_id is None,
            "use_rag_response": False,
            "should_escalate": False,
        }

    async def _exec_condition(
        self, execution: FlowExecution, node: dict
    ) -> dict:
        """
        Execute CONDITION node: evaluate conditions and branch.

        Args:
            execution: Flow execution
            node: Node configuration

        Returns:
            Execution result
        """
        config = node.get("config", {})
        conditions = config.get("conditions", [])

        # Evaluate conditions in order
        for condition in conditions:
            if self._evaluate_condition(condition, execution.collected_data):
                next_node_id = condition.get("next_node_id")
                execution.current_node_id = next_node_id
                self.db.commit()

                # Continue execution (condition nodes don't send messages)
                return await self.execute_node(execution, None)

        # No condition matched - use default path
        default_node_id = config.get("default_node_id")
        execution.current_node_id = default_node_id
        self.db.commit()

        if default_node_id:
            return await self.execute_node(execution, None)
        else:
            return self._error_result("No matching condition and no default path")

    async def _exec_rag_query(
        self, execution: FlowExecution, node: dict
    ) -> dict:
        """
        Execute RAG_QUERY node: call RAG service with interpolated query.

        Args:
            execution: Flow execution
            node: Node configuration

        Returns:
            Execution result
        """
        config = node.get("config", {})
        query_template = config.get("query", "")
        save_to = config.get("save_to", "rag_response")

        # Interpolate variables in query
        query = self._interpolate_variables(query_template, execution.collected_data)

        # Get session for tenant context
        stmt = select(ConversationSession).where(
            ConversationSession.id == execution.session_id
        )
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            return self._error_result("Session not found")

        # Retrieve relevant chunks
        chunks, scores = await self.rag_service.retrieve_relevant_chunks(
            tenant_id=session.tenant_id,
            query=query,
            max_chunks=self.rag_service.MAX_CHUNKS_RETRIEVE,
        )

        # Save results
        rag_result = {
            "query": query,
            "chunks_count": len(chunks),
            "avg_similarity": sum(scores) / len(scores) if scores else 0,
            "chunks": [
                {"id": str(chunk.id), "content": chunk.content} for chunk in chunks
            ],
        }
        execution.collected_data[save_to] = rag_result
        self.db.flush()

        # Advance to next node
        next_node_id = self._get_next_node(node, execution)
        execution.current_node_id = next_node_id
        self.db.commit()

        # Continue execution (RAG nodes don't send messages directly)
        return await self.execute_node(execution, None)

    async def _exec_webhook(
        self, execution: FlowExecution, node: dict
    ) -> dict:
        """
        Execute WEBHOOK node: call external API with httpx.

        Args:
            execution: Flow execution
            node: Node configuration

        Returns:
            Execution result
        """
        config = node.get("config", {})
        url = config.get("url", "")
        method = config.get("method", "POST").upper()
        headers = config.get("headers", {})
        body = config.get("body", {})
        save_to = config.get("save_to", "webhook_response")
        timeout = config.get("timeout", 30)

        # Interpolate variables
        url = self._interpolate_variables(url, execution.collected_data)
        headers = self._interpolate_dict(headers, execution.collected_data)
        body = self._interpolate_dict(body, execution.collected_data)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    response = await client.get(url, headers=headers)
                elif method == "POST":
                    response = await client.post(url, headers=headers, json=body)
                elif method == "PUT":
                    response = await client.put(url, headers=headers, json=body)
                elif method == "DELETE":
                    response = await client.delete(url, headers=headers)
                else:
                    return self._error_result(f"Unsupported HTTP method: {method}")

                # Save response
                webhook_result = {
                    "status_code": response.status_code,
                    "success": 200 <= response.status_code < 300,
                    "data": response.json() if response.text else None,
                }
                execution.collected_data[save_to] = webhook_result
                self.db.flush()

                # Advance to next node
                next_node_id = self._get_next_node(node, execution)
                execution.current_node_id = next_node_id
                self.db.commit()

                # Continue execution
                return await self.execute_node(execution, None)

        except Exception as e:
            logger.error(f"Webhook call failed: {e}", exc_info=True)
            # Save error
            execution.collected_data[save_to] = {
                "status_code": 0,
                "success": False,
                "error": str(e),
            }
            self.db.flush()

            # Try error path if configured
            error_node_id = config.get("error_node_id")
            if error_node_id:
                execution.current_node_id = error_node_id
                self.db.commit()
                return await self.execute_node(execution, None)
            else:
                return self._error_result(f"Webhook failed: {str(e)}")

    async def _exec_escalate(
        self, execution: FlowExecution, node: dict
    ) -> dict:
        """
        Execute ESCALATE node: trigger escalation and create ticket.

        Args:
            execution: Flow execution
            node: Node configuration

        Returns:
            Execution result
        """
        config = node.get("config", {})
        reason_template = config.get("reason", "User requested assistance")
        message_template = config.get(
            "message",
            "I'll connect you with a member of our support team who can help you further.",
        )

        # Interpolate variables
        reason = self._interpolate_variables(reason_template, execution.collected_data)
        message = self._interpolate_variables(
            message_template, execution.collected_data
        )

        # Mark execution as completed
        execution.status = FlowExecutionStatus.COMPLETED
        execution.completed_at = datetime.utcnow()
        execution.result_data = {"escalated": True, "reason": reason}
        self.db.commit()

        return {
            "message": message,
            "buttons": None,
            "is_complete": True,
            "use_rag_response": False,
            "should_escalate": True,
            "escalation_reason": reason,
        }

    async def _exec_end(
        self, execution: FlowExecution, node: dict
    ) -> dict:
        """
        Execute END node: complete flow.

        Args:
            execution: Flow execution
            node: Node configuration

        Returns:
            Execution result
        """
        config = node.get("config", {})
        message_template = config.get("message", "Thank you!")

        # Interpolate variables
        message = self._interpolate_variables(
            message_template, execution.collected_data
        )

        # Mark execution as completed
        execution.status = FlowExecutionStatus.COMPLETED
        execution.completed_at = datetime.utcnow()
        execution.result_data = {
            "completed": True,
            "collected_data": execution.collected_data,
        }
        self.db.commit()

        return {
            "message": message,
            "buttons": None,
            "is_complete": True,
            "use_rag_response": True,  # Allow RAG to handle follow-up questions
            "should_escalate": False,
        }

    def _get_next_node(self, current_node: dict, execution: FlowExecution) -> Optional[str]:
        """
        Get next node ID from current node.

        Args:
            current_node: Current node configuration
            execution: Flow execution

        Returns:
            Next node ID or None if no next node
        """
        config = current_node.get("config", {})
        return config.get("next_node_id")

    def _evaluate_condition(self, condition: dict, data: dict) -> bool:
        """
        Evaluate a single condition against collected data.

        Args:
            condition: Condition configuration
            data: Collected data

        Returns:
            True if condition matches, False otherwise
        """
        variable = condition.get("variable")
        operator = condition.get("operator")
        value = condition.get("value")

        # Get variable value from collected data
        var_value = data.get(variable)

        if operator == "equals":
            return var_value == value
        elif operator == "not_equals":
            return var_value != value
        elif operator == "contains":
            return value in str(var_value) if var_value else False
        elif operator == "not_contains":
            return value not in str(var_value) if var_value else True
        elif operator == "greater_than":
            try:
                return float(var_value) > float(value)
            except (ValueError, TypeError):
                return False
        elif operator == "less_than":
            try:
                return float(var_value) < float(value)
            except (ValueError, TypeError):
                return False
        elif operator == "exists":
            return variable in data
        elif operator == "not_exists":
            return variable not in data
        else:
            logger.warning(f"Unknown condition operator: {operator}")
            return False

    def _validate_input(self, input_value: str, validation: dict) -> dict:
        """
        Validate user input against validation rules.

        Args:
            input_value: User input
            validation: Validation configuration

        Returns:
            Dict with 'valid' bool and optional 'error' message
        """
        validation_type = validation.get("type")

        if validation_type == "required":
            if not input_value or input_value.strip() == "":
                return {"valid": False, "error": "This field is required."}

        elif validation_type == "email":
            email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            if not re.match(email_pattern, input_value):
                return {"valid": False, "error": "Please enter a valid email address."}

        elif validation_type == "phone":
            # Basic phone validation (10+ digits)
            phone_digits = re.sub(r'\D', '', input_value)
            if len(phone_digits) < 10:
                return {"valid": False, "error": "Please enter a valid phone number."}

        elif validation_type == "number":
            try:
                float(input_value)
            except ValueError:
                return {"valid": False, "error": "Please enter a valid number."}

        elif validation_type == "min_length":
            min_len = validation.get("value", 0)
            if len(input_value) < min_len:
                return {
                    "valid": False,
                    "error": f"Input must be at least {min_len} characters.",
                }

        elif validation_type == "max_length":
            max_len = validation.get("value", 1000)
            if len(input_value) > max_len:
                return {
                    "valid": False,
                    "error": f"Input must be at most {max_len} characters.",
                }

        elif validation_type == "regex":
            pattern = validation.get("pattern", "")
            if not re.match(pattern, input_value):
                error_msg = validation.get("error", "Input format is invalid.")
                return {"valid": False, "error": error_msg}

        return {"valid": True}

    def _interpolate_variables(self, template: str, variables: dict) -> str:
        """
        Interpolate variables into template string.

        Supports {{variable}} syntax.

        Args:
            template: Template string with {{variable}} placeholders
            variables: Dictionary of variable values

        Returns:
            Interpolated string
        """
        if not template:
            return ""

        # Find all {{variable}} patterns
        pattern = r'\{\{([^}]+)\}\}'

        def replacer(match):
            var_name = match.group(1).strip()
            # Support nested access with dot notation
            value = variables
            for key in var_name.split('.'):
                if isinstance(value, dict):
                    value = value.get(key, f"{{{{{var_name}}}}}")
                else:
                    return f"{{{{{var_name}}}}}"
            return str(value) if value is not None else f"{{{{{var_name}}}}}"

        return re.sub(pattern, replacer, template)

    def _interpolate_dict(self, data: dict, variables: dict) -> dict:
        """
        Recursively interpolate variables in dictionary values.

        Args:
            data: Dictionary to interpolate
            variables: Variable values

        Returns:
            Interpolated dictionary
        """
        result = {}
        for key, value in data.items():
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

    def _error_result(self, error_message: str) -> dict:
        """
        Create error result.

        Args:
            error_message: Error message

        Returns:
            Error result dict
        """
        logger.error(f"Flow execution error: {error_message}")
        return {
            "message": "I encountered an issue processing your request. Let me connect you with support.",
            "buttons": None,
            "is_complete": True,
            "use_rag_response": False,
            "should_escalate": True,
            "escalation_reason": error_message,
        }
