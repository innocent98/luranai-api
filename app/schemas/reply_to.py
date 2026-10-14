"""Reply-to quote schema (own module: both conversation and visitor schemas need it)."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ReplyToOut(BaseModel):
    """The quote on a reply, frozen at send time. `message_id` is null once the original is gone."""

    model_config = ConfigDict(from_attributes=True)

    message_id: UUID | None = None
    sender_type: Literal["customer", "agent", "ai"]
    kind: Literal["text", "image", "video"]
    snippet: str
