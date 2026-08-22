"""User management routes (API v1)."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import DbSession
from models.users import User
from schemas.user import UserRead
from services.auth.dependencies import get_current_user
from services.billing.gate import maybe_start_mail_sync

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

    # One of the two halves of the start rule. If checkout already happened
    # this is the moment the mailbox sync begins; if it has not, this does
    # nothing and the billing side fires it later. The task re-checks the gate
    # against committed state, so an enqueue that races this request's commit
    # (or survives its rollback) is harmless.
    await maybe_start_mail_sync(db, user)

    return user
