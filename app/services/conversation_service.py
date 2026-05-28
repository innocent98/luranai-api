"""Conversation service for managing chat sessions and messages."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models.conversation import ConversationSession, Message, MessageRole, SessionStatus
from app.db.models.visitor import Visitor
from app.schemas.conversation import (
    ConversationSessionCreate,
    ConversationSessionUpdate,
    MessageResponse,
    SessionDetailResponse,
    SessionResponse,
)
from app.schemas.visitor import VisitorIdentitySummary
from app.utils.attribution import coerce_source, normalize_source_meta
from app.utils.datetime import utcnow_naive


class ConversationService:
    """Service for conversation and message management."""

    def __init__(self, db: Session):
        """Initialize conversation service with database session."""
        self.db = db

    def _build_visitor_summary(self, visitor_id: UUID | None) -> VisitorIdentitySummary | None:
        """Build a support-facing visitor identity summary."""
        if not visitor_id:
            return None

        visitor = self.db.execute(
            select(Visitor).where(Visitor.id == visitor_id)
        ).scalar_one_or_none()
        if not visitor:
            return None

        return VisitorIdentitySummary(
            id=visitor.id,
            name=visitor.name,
            email=visitor.email,
            phone=visitor.phone,
            public_token=visitor.public_token,
            first_seen_at=visitor.first_seen_at,
            last_seen_at=visitor.last_seen_at,
            is_blocked=visitor.is_blocked,
        )

    def _to_session_response(
        self,
        session: ConversationSession,
        include_messages: bool = False,
    ) -> SessionResponse | SessionDetailResponse:
        """Convert DB session model to API response with visitor identity."""
        response: SessionResponse | SessionDetailResponse
        if include_messages:
            response = SessionDetailResponse.model_validate(session)
        else:
            response = SessionResponse.model_validate(session)
        response.visitor = self._build_visitor_summary(session.visitor_id)
        return response

    def create_session(self, session_data: ConversationSessionCreate) -> SessionResponse:
        """
        Create a new conversation session.

        Args:
            session_data: Session creation data

        Returns:
            Created session response
        """
        db_session = ConversationSession(
            tenant_id=session_data.tenant_id,
            user_identifier=session_data.user_identifier,
            visitor_id=session_data.visitor_id,
            visitor_session_id=session_data.visitor_session_id,
            customer_user_id=session_data.customer_user_id,
            status=SessionStatus.ACTIVE,
            meta=session_data.meta or {},
            last_message_at=utcnow_naive(),
            source=coerce_source(session_data.source),
            source_meta=normalize_source_meta(session_data.source_meta),
        )

        self.db.add(db_session)
        self.db.commit()
        self.db.refresh(db_session)

        return self._to_session_response(db_session)

    def get_session(
        self, session_id: UUID, include_messages: bool = False
    ) -> SessionResponse | None:
        """
        Get conversation session by ID.

        Args:
            session_id: Session UUID
            include_messages: Whether to include all messages

        Returns:
            Session response if found, None otherwise
        """
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            return None

        if include_messages:
            return self._to_session_response(session, include_messages=True)
        else:
            return self._to_session_response(session)

    def get_active_session_for_user(
        self, tenant_id: UUID, user_identifier: str
    ) -> SessionResponse | None:
        """
        Get the most recent active session for a user.

        Args:
            tenant_id: Tenant UUID
            user_identifier: User identifier

        Returns:
            Active session if found, None otherwise
        """
        stmt = (
            select(ConversationSession)
            .where(
                ConversationSession.tenant_id == tenant_id,
                ConversationSession.user_identifier == user_identifier,
                ConversationSession.status == SessionStatus.ACTIVE,
            )
            .order_by(desc(ConversationSession.last_message_at))
        )

        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            return None

        return self._to_session_response(session)

    def get_latest_session_for_visitor(
        self,
        tenant_id: UUID,
        visitor_id: UUID,
        allowed_statuses: list[SessionStatus] | None = None,
    ) -> SessionResponse | None:
        """
        Get the most recent session for a visitor.

        Args:
            tenant_id: Tenant UUID
            visitor_id: Visitor UUID
            allowed_statuses: Optional list of session statuses to consider

        Returns:
            Most recent matching session if found, None otherwise
        """
        stmt = select(ConversationSession).where(
            ConversationSession.tenant_id == tenant_id,
            ConversationSession.visitor_id == visitor_id,
        )

        if allowed_statuses:
            stmt = stmt.where(ConversationSession.status.in_(allowed_statuses))

        stmt = stmt.order_by(desc(ConversationSession.last_message_at))

        session = self.db.execute(stmt).scalar_one_or_none()
        if not session:
            return None

        return self._to_session_response(session)

    def get_latest_session_for_customer(
        self,
        tenant_id: UUID,
        customer_user_id: UUID,
        allowed_statuses: list[SessionStatus] | None = None,
    ) -> SessionResponse | None:
        stmt = select(ConversationSession).where(
            ConversationSession.tenant_id == tenant_id,
            ConversationSession.customer_user_id == customer_user_id,
        )
        if allowed_statuses:
            stmt = stmt.where(ConversationSession.status.in_(allowed_statuses))
        stmt = stmt.order_by(desc(ConversationSession.last_message_at))
        session = self.db.execute(stmt).scalar_one_or_none()
        if not session:
            return None
        return self._to_session_response(session)

    def assign_customer_to_session(
        self,
        session_id: UUID,
        customer_user_id: UUID,
    ) -> SessionResponse:
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()
        if not session:
            raise ValueError("Session not found")

        session.customer_user_id = customer_user_id
        self.db.commit()
        self.db.refresh(session)
        return self._to_session_response(session)

    def update_session(
        self, session_id: UUID, session_data: ConversationSessionUpdate
    ) -> SessionResponse:
        """
        Update conversation session.

        Args:
            session_id: Session UUID
            session_data: Updated session data

        Returns:
            Updated session response

        Raises:
            ValueError: If session not found
        """
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        # Update fields
        update_data = session_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(session, field, value)

        self.db.commit()
        self.db.refresh(session)

        return self._to_session_response(session)

    def escalate_session(self, session_id: UUID, reason: str) -> SessionResponse:
        """
        Mark a session as escalated.

        Args:
            session_id: Session UUID
            reason: Escalation reason

        Returns:
            Updated session response

        Raises:
            ValueError: If session not found
        """
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        session.status = SessionStatus.ESCALATED
        session.escalation_reason = reason
        session.escalated_at = utcnow_naive()

        self.db.commit()
        self.db.refresh(session)

        return self._to_session_response(session)

    def update_session_meta(self, session_id: UUID, meta: dict) -> SessionResponse:
        """
        Update session metadata.

        Args:
            session_id: Session UUID
            meta: Updated metadata dictionary

        Returns:
            Updated session response

        Raises:
            ValueError: If session not found
        """
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        session.meta = meta

        self.db.commit()
        self.db.refresh(session)

        return SessionResponse.model_validate(session)

    def add_message(
        self,
        session_id: UUID,
        role: MessageRole,
        content: str,
        retrieved_chunk_ids: list[UUID] | list[str] | None = None,
        token_count: int | None = None,
        meta: dict | None = None,
    ) -> MessageResponse:
        """
        Add a message to a conversation session.

        Args:
            session_id: Session UUID
            role: Message role (user/assistant/system)
            content: Message content
            retrieved_chunk_ids: Optional knowledge chunk IDs used
            token_count: Optional token count
            meta: Optional metadata

        Returns:
            Created message response

        Raises:
            ValueError: If session not found
        """
        # Verify session exists
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        # Create message
        db_message = Message(
            session_id=session_id,
            role=role,
            content=content,
            retrieved_chunk_ids=retrieved_chunk_ids,
            token_count=token_count,
            meta=meta or {},
        )

        self.db.add(db_message)

        # Update session last_message_at
        session.last_message_at = utcnow_naive()

        self.db.commit()
        self.db.refresh(db_message)

        return MessageResponse.model_validate(db_message)

    def get_session_messages(
        self, session_id: UUID, limit: int | None = None
    ) -> list[MessageResponse]:
        """
        Get messages for a session.

        Args:
            session_id: Session UUID
            limit: Optional limit on number of messages (returns most recent)

        Returns:
            List of message responses ordered by creation time
        """
        stmt = select(Message).where(Message.session_id == session_id).order_by(Message.created_at)

        if limit:
            # Get last N messages
            stmt = (
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(desc(Message.created_at))
                .limit(limit)
            )
            messages = list(reversed(self.db.execute(stmt).scalars().all()))
        else:
            messages = list(self.db.execute(stmt).scalars().all())

        return [MessageResponse.model_validate(msg) for msg in messages]

    def get_session_messages_paginated(
        self,
        session_id: UUID,
        limit: int = 50,
        before: datetime | None = None,
    ) -> tuple[list[MessageResponse], bool]:
        """
        Get paginated messages for a session, newest last.

        Args:
            session_id: Session UUID
            limit: Max messages to return
            before: Only return messages created before this timestamp (cursor)

        Returns:
            Tuple of (messages ordered oldest->newest, has_more bool)
        """
        stmt = select(Message).where(Message.session_id == session_id)
        if before:
            stmt = stmt.where(Message.created_at < before)

        # Get limit+1 to determine has_more, ordered newest first
        stmt = stmt.order_by(desc(Message.created_at)).limit(limit + 1)
        results = list(self.db.execute(stmt).scalars().all())

        has_more = len(results) > limit
        if has_more:
            results = results[:limit]

        # Reverse to oldest->newest for display order
        results.reverse()

        return [MessageResponse.model_validate(msg) for msg in results], has_more

    def get_recent_context(self, session_id: UUID, max_turns: int = 10) -> list[MessageResponse]:
        """
        Get recent conversation context (last N turns).

        Args:
            session_id: Session UUID
            max_turns: Maximum number of conversation turns (user + assistant pairs)

        Returns:
            List of recent messages
        """
        # Get last (max_turns * 2) messages to capture user-assistant pairs
        return self.get_session_messages(session_id, limit=max_turns * 2)

    def get_tenant_sessions(
        self, tenant_id: UUID, status: SessionStatus | None = None, limit: int = 100
    ) -> list[SessionResponse]:
        """
        Get sessions for a tenant.

        Args:
            tenant_id: Tenant UUID
            status: Optional status filter
            limit: Maximum number of sessions to return

        Returns:
            List of session responses
        """
        stmt = select(ConversationSession).where(ConversationSession.tenant_id == tenant_id)

        if status:
            stmt = stmt.where(ConversationSession.status == status)

        stmt = stmt.order_by(desc(ConversationSession.last_message_at)).limit(limit)

        sessions = self.db.execute(stmt).scalars().all()

        return [self._to_session_response(s) for s in sessions]

    def assign_agent_to_session(self, session_id: UUID, agent_id: UUID) -> SessionResponse:
        """
        Assign a support agent to a conversation session.

        Args:
            session_id: Session UUID
            agent_id: Agent user ID

        Returns:
            Updated session response

        Raises:
            ValueError: If session not found
        """
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        session.agent_id = agent_id
        session.agent_joined_at = utcnow_naive()

        self.db.commit()
        self.db.refresh(session)

        return self._to_session_response(session)

    def remove_agent_from_session(self, session_id: UUID) -> SessionResponse:
        """
        Remove agent assignment from a conversation session.

        Args:
            session_id: Session UUID

        Returns:
            Updated session response

        Raises:
            ValueError: If session not found
        """
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session:
            raise ValueError("Session not found")

        session.agent_id = None
        session.agent_joined_at = None

        self.db.commit()
        self.db.refresh(session)

        return self._to_session_response(session)

    def mark_conversation_read(self, session_id: UUID, customer_user_id: UUID) -> bool:
        """Mark a conversation as read by the customer."""
        from datetime import UTC

        session = (
            self.db.query(ConversationSession)
            .filter(
                ConversationSession.id == session_id,
            )
            .first()
        )
        if not session:
            return False
        if session.customer_user_id is None:
            session.customer_user_id = customer_user_id
        elif session.customer_user_id != customer_user_id:
            return False
        session.customer_last_read_at = datetime.now(UTC)
        self.db.commit()
        return True
