"""User management routes (API v1)."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import DbSession
from models.users import User
from schemas.user import UserRead
from services.auth.dependencies import get_current_user

router = APIRouter(prefix="/users", tags=["users"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("/me/onboarding/complete", response_model=UserRead)
async def complete_onboarding(user: CurrentUser, db: DbSession) -> User:
    """Mark the onboarding wizard finished.

    Idempotent: a repeat call (double-submit, or a retry after a network blip)
    leaves the original timestamp alone rather than moving it.
    """
    if user.onboarded_at is None:
        user.onboarded_at = datetime.now(timezone.utc)
    return user
