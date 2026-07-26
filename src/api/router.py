"""Aggregates every v1 router under the versioned API prefix."""

from fastapi import APIRouter

from api.v1 import auth, integrations, mailman, users, webhooks

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(integrations.router)
api_router.include_router(mailman.router)
api_router.include_router(webhooks.router)
