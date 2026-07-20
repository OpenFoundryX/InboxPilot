"""User management routes (API v1)."""

from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])
