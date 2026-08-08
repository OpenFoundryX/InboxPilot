"""Aggregates every v1 router under the versioned API prefix."""

from fastapi import APIRouter

from api.v1 import (
    auth,
    billing,
    categorization,
    chat,
    dashboard,
    drafts,
    integrations,
    mailman,
    meetings,
    notes,
    scheduling,
    scheduling_public,
    users,
    webhooks,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(integrations.router)
api_router.include_router(mailman.router)
api_router.include_router(categorization.router)
api_router.include_router(drafts.router)
api_router.include_router(chat.router)
api_router.include_router(dashboard.router)
api_router.include_router(meetings.router)
api_router.include_router(notes.router)
api_router.include_router(webhooks.router)
api_router.include_router(billing.router)
api_router.include_router(scheduling.router)
# Public before nothing in particular, but kept adjacent to its authenticated
# half so a reader sees both halves of the feature in one place.
api_router.include_router(scheduling_public.router)
api_router.include_router(scheduling_public.manage_router)
