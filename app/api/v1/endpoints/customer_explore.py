"""Customer-facing explore endpoints."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_customer, get_db
from app.schemas.customer_auth import CustomerUserResponse
from app.schemas.explore import PersonalBusinessesResponse
from app.services.customer_explore_service import CustomerExploreService

router = APIRouter()


@router.get(
    "/personal-businesses",
    response_model=PersonalBusinessesResponse,
    status_code=status.HTTP_200_OK,
)
def list_personal_businesses(
    response: Response,
    current_customer: CustomerUserResponse = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> PersonalBusinessesResponse:
    """Return up to 8 followed/recently-chatted businesses for the authenticated customer."""
    response.headers["Cache-Control"] = "private, max-age=120, stale-while-revalidate=300"
    service = CustomerExploreService(db)
    return service.get_personal_businesses(customer_user_id=current_customer.id)
