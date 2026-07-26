"""Shared Composio SDK client.

One lazily-constructed client per process, built from settings. Integration
modules (e.g. `integrations.gmail`) import `get_composio()` rather than
constructing their own client.
"""

from functools import lru_cache

from composio import Composio
from composio.core.models.base import allow_tracking

from core.config import settings


allow_tracking.set(False)


@lru_cache
def get_composio() -> Composio:
    """Get the Composio SDK client."""
    kwargs: dict = {
        "api_key": settings.COMPOSIO_API_KEY,
        "allow_tracking": False,
        "toolkit_versions": {
            "gmail": settings.COMPOSIO_GMAIL_TOOLKIT_VERSION,
            "googlecalendar": settings.COMPOSIO_GCAL_TOOLKIT_VERSION,
        },
    }

    return Composio(**kwargs)
