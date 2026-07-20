"""Shared FastAPI dependencies.

Re-exports the common dependencies so routers can import them from one place.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db

DbSession = Annotated[AsyncSession, Depends(get_db)]

__all__ = ["DbSession", "get_db"]
