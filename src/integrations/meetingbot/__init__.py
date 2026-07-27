"""Meeting-bot provider selection.

One place resolves `MEETING_BOT_PROVIDER` to an implementation, so callers never
name a vendor. Adding one means a new module and a line in `_PROVIDERS`.
"""

from functools import lru_cache

from core.config import settings
from integrations.meetingbot.base import MeetingBotError, MeetingBotProvider
from integrations.meetingbot.recall import RecallProvider

_PROVIDERS = {"recall": RecallProvider}


@lru_cache(maxsize=1)
def get_provider() -> MeetingBotProvider:
    name = (settings.MEETING_BOT_PROVIDER or "").strip().lower()
    factory = _PROVIDERS.get(name)
    if not factory:
        raise MeetingBotError(
            f"Unknown MEETING_BOT_PROVIDER {name!r}; expected one of {sorted(_PROVIDERS)}"
        )
    return factory()


__all__ = ["get_provider", "MeetingBotError", "MeetingBotProvider"]
