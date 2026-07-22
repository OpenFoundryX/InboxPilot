"""Calendar routines: a heads-up when meetings collide."""

from core.logging import get_logger
from integrations.composio import calendar
from services.notify import send_to_inbox

log = get_logger(__name__)


def double_bookings_digest(user_id: str, email: str, tz: str, days: int = 1) -> int:
    """Email a heads-up if any meetings overlap in the next `days` days."""
    if not calendar.is_connected(user_id):
        return 0
    clashes = calendar.find_double_bookings(user_id, tz, days=days)
    if not clashes:
        return 0
    lines = [f"  • “{a}”  ⟷  “{b}”" for a, b in clashes]
    body = (
        "Heads-up — these meetings overlap:\n\n"
        + "\n".join(lines)
        + "\n\nYou may want to move one.\n\n— InboxOS"
    )
    send_to_inbox(user_id, email, f"⚠️ {len(clashes)} calendar clash(es) ahead", body)
    log.info("calendar.double_bookings_sent", user_id=user_id, count=len(clashes))
    return len(clashes)
