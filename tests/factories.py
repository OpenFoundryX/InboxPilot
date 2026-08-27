"""Builders for test rows. Keep them minimal — a factory that sets more than a
test needs makes the test lie about what actually mattered."""

import uuid

from models.users import User


async def make_user(db, **overrides) -> User:
    defaults = {
        "email": f"user-{uuid.uuid4().hex[:12]}@example.com",
        "full_name": "Test User",
    }
    user = User(**{**defaults, **overrides})
    db.add(user)
    await db.flush()
    return user
