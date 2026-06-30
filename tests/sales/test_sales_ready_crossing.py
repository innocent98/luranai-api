from uuid import uuid4
from app.db.models.lead import Lead
from app.db.models.tenant import Tenant
from app.services.lead_service import LeadService


def test_crossing(db):
    svc = LeadService(db)
    lead = Lead(tenant_id=uuid4())
    lead.lead_score = 90
    assert svc.crossed_sales_ready(None, lead) is True   # new lead lands sales-ready
    assert svc.crossed_sales_ready(50, lead) is True     # warm → sales-ready
    assert svc.crossed_sales_ready(88, lead) is False    # already sales-ready
    lead.lead_score = 70
    assert svc.crossed_sales_ready(50, lead) is False    # didn't reach 86
