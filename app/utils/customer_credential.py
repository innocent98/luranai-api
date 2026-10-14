"""Customer credential check for the public chat write paths.

A session id alone is not a credential: staff can read it. A caller proves they are the
customer with any one of:

* the session's `user_identifier` (what the customer sockets already require),
* the session's visitor token (`visitor_token`, resolved against the session's visitor),
* an authenticated customer account that owns the session.

Staff tokens never satisfy this check, whatever tenant they belong to.
"""

import hmac
from uuid import UUID

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import extract_customer_token, is_token_revoked
from app.core.config import settings
from app.db.models.conversation import ConversationSession
from app.db.models.customer_user import CustomerUser
from app.db.models.visitor import Visitor

INVALID_CREDENTIAL_DETAIL = "Invalid customer credential"


def invalid_credential() -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail=INVALID_CREDENTIAL_DETAIL)


def _equal(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode(), supplied.encode())


def optional_customer(request: Request, db: Session) -> CustomerUser | None:
    """The authenticated customer account behind the request, or None (never raises)."""
    token = extract_customer_token(request)
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("auth_type") != "customer":
            return None
        jti = payload.get("jti")
        if jti and is_token_revoked(jti):
            return None
        customer = db.get(CustomerUser, UUID(str(payload.get("sub"))))
    except (JWTError, ValueError, TypeError):
        return None
    return customer if customer is not None and customer.is_active else None


def session_credential_matches(
    db: Session,
    session: ConversationSession,
    *,
    user_identifier: str | None = None,
    visitor_token: str | None = None,
    customer: CustomerUser | None = None,
) -> bool:
    if _equal(session.user_identifier, user_identifier):
        return True
    if customer is not None and (
        session.customer_user_id == customer.id
        or _equal(session.user_identifier, customer.chat_user_key)
    ):
        return True
    if isinstance(visitor_token, str) and visitor_token.strip() and session.visitor_id:
        visitor = db.execute(
            select(Visitor).where(
                Visitor.id == session.visitor_id,
                Visitor.tenant_id == session.tenant_id,
            )
        ).scalar_one_or_none()
        if visitor is not None and _equal(visitor.public_token, visitor_token.strip()):
            return True
    return False
