"""QuoteService — persist a computed quotation and link it to the lead (Stage 3 Slice 4)."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.lead import Lead
from app.db.models.quote import Quote, QuoteStatus


class QuoteService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_quote(
        self,
        tenant_id: UUID,
        lead: Lead,
        *,
        currency: str,
        line_items: list,
        total: int,
    ) -> Quote:
        quote = Quote(
            tenant_id=tenant_id,
            lead_id=lead.id,
            conversation_id=lead.conversation_id,
            currency=currency,
            line_items=line_items,
            total=total,
            status=QuoteStatus.DRAFT,
        )
        self.db.add(quote)
        self.db.flush()
        lead.quote_id = quote.id
        self.db.commit()
        self.db.refresh(quote)
        return quote
