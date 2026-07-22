import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import create_access_token, hash_refresh_token, new_refresh_token
from models.auth import RefreshToken
from models.users import User


async def upsert_user_from_google(db: AsyncSession, profile: dict) -> User:
    user = await db.scalar(select(User).where(User.google_sub == profile["sub"]))
    now = datetime.now(timezone.utc)
    if user is None:  # ← signup
        user = User(
            google_sub=profile["sub"],
            email=profile["email"],
            full_name=profile.get("name"),
            picture=profile.get("picture"),
            last_login_at=now,
        )
        db.add(user)
    else:
        user.email = profile["email"]
        user.full_name = profile.get("name")
        user.picture = profile.get("picture")
        user.last_login_at = now
    await db.commit()
    await db.refresh(user)
    return user


async def issue_tokens(db: AsyncSession, user: User) -> tuple[str, str]:
    access = create_access_token(user.id)
    raw_refresh, refresh_hash = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS),
        )
    )
    await db.commit()
    return access, raw_refresh


async def rotate_refresh_token(db: AsyncSession, raw_refresh: str) -> tuple[str, str] | None:
    """Validate + rotate. Returns new (access, refresh) or None if invalid."""

    row = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_refresh)))

    if row is None or row.revoked or row.expires_at < datetime.now(timezone.utc):
        return None

    row.revoked = True
    user = await db.get(User, row.user_id)
    await db.commit()
    return await issue_tokens(db, user)


async def revoke_all(db: AsyncSession, user_id: uuid.UUID) -> None:

    rows = await db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked.is_(False),
        )
    )

    for row in rows:
        row.revoked = True

    await db.commit()
