"""Gmail server-side filter that holds incoming mail out of the inbox.

This is what makes batching notification-free: the filter runs on Google's side
the moment mail is delivered, so non-VIP mail never enters the inbox (and Gmail
never fires an arrival notification). VIP senders/keywords are excluded via the
filter's negatedQuery, so they land in the inbox and notify normally.
"""

from fastapi import status

from core.exceptions import AppError
from core.logging import get_logger
from integrations.composio import gmail
from integrations.composio.composio_client import get_composio
from services.mailman import gmail_ops

log = get_logger(__name__)

CREATE_FILTER = "GMAIL_CREATE_FILTER"
DELETE_FILTER = "GMAIL_DELETE_FILTER"
LIST_FILTERS = "GMAIL_LIST_FILTERS"

# Matches everything the user *receives* (i.e. not sent by them). Gmail filters
# need at least one criteria field; this is our catch-all.
INBOUND_QUERY = "-from:me"


class HoldLabelMissing(AppError):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    detail = "could not resolve the inboxos-later label"


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


def apply_hold_filter(
    user_id: str,
    *,
    domains: list[str],
    addresses: list[str],
    keywords: list[str],
    existing_filter_id: str | None,
) -> str:
    """Install/replace the skip-inbox filter. Returns the new Gmail filter id.

    Blocking Composio calls — invoke from a Celery task or a threadpool.
    """
    if existing_filter_id:
        try:
            delete_filter(user_id, existing_filter_id)
        except Exception:
            log.warning(
                "mailman.hold_filter_delete_failed",
                user_id=user_id,
                filter_id=existing_filter_id,
                exc_info=True,
            )

    # ensure_labels already listed (and created) every label, so it knows the
    # hold label's id — take it from there rather than spending another
    # LIST_LABELS round trip on Composio, and warm the shared cache while we
    # have the authoritative ids in hand.
    sync = gmail.ensure_labels(user_id)
    gmail_ops.cache_label_ids(user_id, sync.ids)

    hold_label_id = sync.ids.get(gmail_ops.HOLD_LABEL_NAME.casefold())
    if not hold_label_id:
        raise HoldLabelMissing()

    filter_id = create_hold_filter(
        user_id,
        hold_label_id,
        domains=domains,
        addresses=addresses,
        keywords=keywords,
    )
    if not filter_id:
        raise RuntimeError("create_hold_filter returned no id")

    return filter_id


def remove_hold_filter(user_id: str, filter_id: str | None) -> None:
    """Delete the skip-inbox filter if present. Best-effort; logs on failure."""
    if not filter_id:
        return
    try:
        delete_filter(user_id, filter_id)
    except Exception:
        log.warning(
            "mailman.hold_filter_delete_failed",
            user_id=user_id,
            filter_id=filter_id,
            exc_info=True,
        )


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
        error = resp.get("error")
        if not _is_already_exists(error):
            raise RuntimeError(f"Composio {CREATE_FILTER} failed: {error}")

        # Gmail refuses a byte-identical filter instead of returning the one it
        # already has. We land here whenever the stored id has drifted from
        # reality — the delete above failed, or the settings row was reset while
        # the filter survived in Gmail. Returning None is not an option: the
        # caller persists this id, and without it /mailman/stop could never
        # remove the filter, leaving mail skipping the inbox forever. So adopt
        # the live filter instead, which also repairs the stored id.
        #
        # Gmail only raises this when criteria *and* action both match, so the
        # filter we find is the one we were about to create — the VIP rules in
        # it are the ones the caller just asked for, not stale ones.
        existing = find_hold_filter(user_id, hold_label_id, criteria)
        if existing is None:
            raise RuntimeError(
                f"Composio {CREATE_FILTER} reported the filter already exists, "
                f"but no live filter matches it: {error}"
            )
        log.info("mailman.hold_filter_adopted", user_id=user_id, filter_id=existing)
        return existing

    data = resp.get("data") or {}
    filter_id = data.get("id") or (data.get("response_data") or {}).get("id")
    log.info("mailman.hold_filter_created", user_id=user_id, filter_id=filter_id)
    return filter_id


def _is_already_exists(error: object) -> bool:
    """True for Gmail's 400 FAILED_PRECONDITION "Filter already exists".

    Composio hands the error back as the raw Google error dict rather than a
    typed failure, so matching on its text is the only handle we have.
    """
    lowered = str(error or "").casefold()
    return "already exists" in lowered or "failed_precondition" in lowered


def find_hold_filter(user_id: str, hold_label_id: str, criteria: dict) -> str | None:
    """Find the live hold filter matching `criteria`; return its id, or None."""
    resp = get_composio().tools.execute(LIST_FILTERS, {}, user_id=user_id)
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {LIST_FILTERS} failed: {resp.get('error')}")

    data = resp.get("data") or {}
    # Gmail names the array "filter", singular, and Composio sometimes nests
    # the payload a level deeper.
    rows = data.get("filter") or (data.get("response_data") or {}).get("filter") or []

    for row in rows:
        act = row.get("action") or {}
        if hold_label_id not in (act.get("addLabelIds") or []):
            continue
        if "INBOX" not in (act.get("removeLabelIds") or []):
            continue
        live = row.get("criteria") or {}
        if live.get("query") == criteria.get("query") and live.get(
            "negatedQuery"
        ) == criteria.get("negatedQuery"):
            return row.get("id")
    return None


def delete_filter(user_id: str, filter_id: str) -> None:
    if not filter_id:
        return
    get_composio().tools.execute(
        DELETE_FILTER, {"filter_id": filter_id}, user_id=user_id
    )
    log.info("mailman.hold_filter_deleted", user_id=user_id, filter_id=filter_id)
