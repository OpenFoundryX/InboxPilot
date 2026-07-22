"""Gmail server-side filter that holds incoming mail out of the inbox.

This is what makes batching notification-free: the filter runs on Google's side
the moment mail is delivered, so non-VIP mail never enters the inbox (and Gmail
never fires an arrival notification). VIP senders/keywords are excluded via the
filter's negatedQuery, so they land in the inbox and notify normally.
"""

from core.logging import get_logger
from integrations.composio.composio_client import get_composio

log = get_logger(__name__)

CREATE_FILTER = "GMAIL_CREATE_FILTER"
DELETE_FILTER = "GMAIL_DELETE_FILTER"

# Matches everything the user *receives* (i.e. not sent by them). Gmail filters
# need at least one criteria field; this is our catch-all.
INBOUND_QUERY = "-from:me"


def vip_query(domains: list[str], addresses: list[str], keywords: list[str]) -> str:
    """Build a Gmail search expression matching VIP mail (to be *excluded*)."""
    parts: list[str] = []
    senders = [s for s in (domains + addresses) if s]
    if senders:
        parts.append("from:(" + " OR ".join(senders) + ")")
    kws = [k for k in keywords if k]
    if kws:
        parts.append("(" + " OR ".join(kws) + ")")
    return " OR ".join(parts)


def create_hold_filter(
    user_id: str,
    hold_label_id: str,
    *,
    domains: list[str],
    addresses: list[str],
    keywords: list[str],
) -> str | None:
    """Create the skip-inbox filter for a user; return its Gmail filter id."""
    criteria: dict = {"query": INBOUND_QUERY}
    negated = vip_query(domains, addresses, keywords)
    if negated:
        criteria["negatedQuery"] = negated

    action = {"removeLabelIds": ["INBOX"], "addLabelIds": [hold_label_id]}

    resp = get_composio().tools.execute(
        CREATE_FILTER, {"criteria": criteria, "action": action}, user_id=user_id
    )
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {CREATE_FILTER} failed: {resp.get('error')}")

    data = resp.get("data") or {}
    filter_id = data.get("id") or (data.get("response_data") or {}).get("id")
    log.info("mailman.hold_filter_created", user_id=user_id, filter_id=filter_id)
    return filter_id


def delete_filter(user_id: str, filter_id: str) -> None:
    if not filter_id:
        return
    get_composio().tools.execute(
        DELETE_FILTER, {"filter_id": filter_id}, user_id=user_id
    )
    log.info("mailman.hold_filter_deleted", user_id=user_id, filter_id=filter_id)
