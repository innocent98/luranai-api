"""Pydantic schemas for in-app notification read API (Stage 3 Slice 5c)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class NotificationItem(BaseModel):
    id: UUID
    event: str
    title: str | None
    body: str | None
    read_at: datetime | None
    created_at: datetime


class NotificationList(BaseModel):
    items: list[NotificationItem]
    total: int
    limit: int
    offset: int
