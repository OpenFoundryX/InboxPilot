"""Dashboard home — one aggregate payload for the landing page."""

from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import DbSession
from models.users import User
from schemas.dashboard import DashboardSummary
from services.auth.dependencies import get_current_user
from services.dashboard import summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(user: CurrentUser, db: DbSession) -> DashboardSummary:
    """Everything the dashboard home renders, in one round trip.

    One endpoint rather than a client-side fan-out so the server owns day
    boundaries: the browser's clock must not get to disagree with the backend
    about what "tomorrow" means.
    """
    return await summary.build_summary(db, user)
