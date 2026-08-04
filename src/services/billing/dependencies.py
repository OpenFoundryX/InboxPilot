"""FastAPI glue over `entitlements.check`.

A denial is 402 Payment Required rather than 403: the client distinguishes
"you need to pay" from "you may not do this" to decide whether to show the
plan picker.
"""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from models.users import User
from services.auth.dependencies import get_current_user
from services.billing.access import ACCESS_ENTITLED, resolve_access
from services.billing.store import get_subscription


async def require_entitled(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Reject users whose subscription no longer grants access."""
    sub = await get_subscription(db, user.id)
    if resolve_access(sub, datetime.now(timezone.utc)) != ACCESS_ENTITLED:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Your subscription is not active.",
        )
    return user


EntitledUser = Annotated[User, Depends(require_entitled)]
