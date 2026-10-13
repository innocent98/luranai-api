"""Customer presence schema (staff-facing only — never on customer payloads)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PresenceState = Literal["active", "away", "offline"]


class CustomerPresence(BaseModel):
    """Customer presence as staff see it.

    ``last_seen_at`` is only set when ``state`` is ``offline`` (and known); for
    ``active``/``away`` the customer is connected now, so it is null.
    """

    state: PresenceState
    last_seen_at: datetime | None = Field(
        None, description="Last time the customer was connected; set only when offline"
    )
