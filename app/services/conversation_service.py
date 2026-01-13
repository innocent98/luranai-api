"""Conversation service for managing chat sessions and messages."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import select, desc

from app.db.models.conversation import (
    ConversationSession,
    Message,
    SessionStatus,
    MessageRole,
)
from app.schemas.conversation import (
    ConversationSessionCreate,
    ConversationSessionUpdate,
    SessionResponse,
    SessionDetailResponse,
    MessageCreate,
    MessageResponse,
)


class ConversationService:
    """Service for conversation and message management."""

    def __init__(self, db: Session):
        """Initialize conversation service with database session."""
        self.db = db

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
            status=SessionStatus.ACTIVE,
            meta=session_data.meta or {},
            confidence_scores=[],
            last_message_at=datetime.utcnow(),
        )

        self.db.add(db_session)
        self.db.commit()
        self.db.refresh(db_session)

        return SessionResponse.model_validate(db_session)

    def get_session(self, session_id: UUID, include_messages: bool = False) -> Optional[SessionResponse]:
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
            return SessionDetailResponse.model_validate(session)
        else:
            return SessionResponse.model_validate(session)

    def get_active_session_for_user(
        self, tenant_id: UUID, user_identifier: str
    ) -> Optional[SessionResponse]:
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

        return SessionResponse.model_validate(session)

    def update_session(self, session_id: UUID, session_data: ConversationSessionUpdate) -> SessionResponse:
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

        return SessionResponse.model_validate(session)

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
        session.escalated_at = datetime.utcnow()

        self.db.commit()
        self.db.refresh(session)

        return SessionResponse.model_validate(session)

    def add_message(
        self,
        session_id: UUID,
        role: MessageRole,
        content: str,
        retrieved_chunk_ids: Optional[list[UUID]] = None,
        confidence_score: Optional[float] = None,
        token_count: Optional[int] = None,
        meta: Optional[dict] = None,
    ) -> MessageResponse:
        """
        Add a message to a conversation session.

        Args:
            session_id: Session UUID
            role: Message role (user/assistant/system)
            content: Message content
            retrieved_chunk_ids: Optional knowledge chunk IDs used
            confidence_score: Optional AI confidence score
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
            confidence_score=confidence_score,
            token_count=token_count,
            meta=meta or {},
        )

        self.db.add(db_message)

        # Update session last_message_at
        session.last_message_at = datetime.utcnow()

        # Track confidence scores for assistant messages
        if role == MessageRole.ASSISTANT and confidence_score is not None:
            if session.confidence_scores is None:
                session.confidence_scores = []
            session.confidence_scores.append(confidence_score)

        self.db.commit()
        self.db.refresh(db_message)

        return MessageResponse.model_validate(db_message)

    def get_session_messages(
        self, session_id: UUID, limit: Optional[int] = None
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
            messages = self.db.execute(stmt).scalars().all()

        return [MessageResponse.model_validate(msg) for msg in messages]

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
        self, tenant_id: UUID, status: Optional[SessionStatus] = None, limit: int = 100
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

        return [SessionResponse.model_validate(s) for s in sessions]

    def get_average_confidence(self, session_id: UUID) -> Optional[float]:
        """
        Calculate average confidence score for a session.

        Args:
            session_id: Session UUID

        Returns:
            Average confidence score or None if no scores
        """
        stmt = select(ConversationSession).where(ConversationSession.id == session_id)
        session = self.db.execute(stmt).scalar_one_or_none()

        if not session or not session.confidence_scores:
            return None

        return sum(session.confidence_scores) / len(session.confidence_scores)
