"""Gmail label operations for holding/releasing mail (Composio, blocking).

Holding a message = pull it out of the inbox by removing the INBOX label and
adding our holding label (`inboxos-later`). Releasing = the reverse. Call these
from Celery tasks or a threadpool.
"""

from integrations.composio.composio_client import get_composio
from integrations.composio.gmail import FETCH_EMAILS, LIST_LABELS

BATCH_MODIFY = "GMAIL_BATCH_MODIFY_MESSAGES"
INBOX_LABEL = "INBOX"
UNREAD_LABEL = "UNREAD"
STARRED_LABEL = "STARRED"
HOLD_LABEL_NAME = "inboxos-later"

# Per-process cache of resolved label ids, keyed by (user_id, casefolded name).
# We deliberately cache ONLY successful resolutions. Caching a miss would be a
# footgun: a transient LIST_LABELS failure — or a lookup that happens to run
# before a label is provisioned — would pin `None` for the whole worker process,
# making every later add_label for that label fail even once the label exists.
_LABEL_ID_CACHE: dict[tuple[str, str], str] = {}


def cache_label_ids(user_id: str, ids: dict[str, str]) -> None:
    """Warm the cache from label ids a caller already has in hand.

    Lets a caller that just listed labels for its own reasons (e.g.
    `gmail.ensure_labels`) hand the result over instead of forcing
    `resolve_label_id` into a second, identical round trip. Keys are
    casefolded label names.
    """
    for name, lid in ids.items():
        if name and lid:
            _LABEL_ID_CACHE[(user_id, name)] = lid


def resolve_label_id(user_id: str, name: str) -> str | None:
    """Resolve a Gmail label name to its id for a user (successful lookups cached).

    On a cache miss we re-list the account's labels and warm the cache with every
    label found, so a single LIST call resolves many names. A genuine miss is not
    cached — the next call lists again, which self-heals once the label appears.
    """
    key = (user_id, name.casefold())
    if hit := _LABEL_ID_CACHE.get(key):
        return hit
    resp = get_composio().tools.execute(LIST_LABELS, {}, user_id=user_id)
    if resp.get("successful") is False:
        return None
    cache_label_ids(
        user_id,
        {
            (label.get("name") or "").casefold(): label.get("id")
            for label in (resp.get("data") or {}).get("labels") or []
        },
    )
    return _LABEL_ID_CACHE.get(key)


def clear_label_cache() -> None:
    """Drop all cached label-id resolutions (e.g. after creating/deleting labels)."""
    _LABEL_ID_CACHE.clear()


def _modify(user_id: str, message_ids: list[str], add: list[str], remove: list[str]) -> None:
    if not message_ids:
        return
    resp = get_composio().tools.execute(
        BATCH_MODIFY,
        {"messageIds": message_ids, "addLabelIds": add, "removeLabelIds": remove},
        user_id=user_id,
    )
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {BATCH_MODIFY} failed: {resp.get('error')}")


def add_label(user_id: str, message_ids: list[str], label_name: str) -> None:
    """Apply a label (by name) to messages, resolving its id first."""
    label_id = resolve_label_id(user_id, label_name)
    if not label_id:
        raise RuntimeError(f"label {label_name!r} not found for user {user_id}")
    _modify(user_id, message_ids, add=[label_id], remove=[])


def apply_category(
    user_id: str,
    message_ids: list[str],
    label_name: str,
    actions: dict[str, bool] | None = None,
) -> None:
    """Apply a category label and its side effects in one Composio round trip.

    `actions` keys are `archive`, `mark_read`, and `star` (see
    `models.categorization.CATEGORY_ACTIONS`). Batching them with the label is
    the point: four separate calls per message would quadruple the Gmail cost of
    classification.
    """
    label_id = resolve_label_id(user_id, label_name)
    if not label_id:
        raise RuntimeError(f"label {label_name!r} not found for user {user_id}")

    actions = actions or {}
    add = [label_id]
    remove = []
    if actions.get("archive"):
        remove.append(INBOX_LABEL)
    if actions.get("mark_read"):
        remove.append(UNREAD_LABEL)
    if actions.get("star"):
        add.append(STARRED_LABEL)

    # Belt-and-braces: a category label can resolve to the same id as one of
    # the action labels above (e.g. a mis-provisioned/legacy category sharing
    # a Gmail system label id). A label id must never appear in both lists in
    # one batchModify, so drop anything in `add` from `remove`.
    remove = [lid for lid in remove if lid not in add]

    _modify(user_id, message_ids, add=add, remove=remove)


def remove_labels(user_id: str, message_ids: list[str], label_names: list[str]) -> None:
    """Remove the named labels from messages (ignores ones that don't exist)."""
    ids = [lid for n in label_names if (lid := resolve_label_id(user_id, n))]
    if ids:
        _modify(user_id, message_ids, add=[], remove=ids)


def deliver_to_inbox(user_id: str, message_ids: list[str], also_label: str | None = None) -> None:
    """Ensure messages carry INBOX (API self-sends land in Sent only).

    Optionally also applies a named label (e.g. inboxos-chat so the command
    sweep ignores our own outgoing mail).
    """
    add = [INBOX_LABEL]
    if also_label and (lid := resolve_label_id(user_id, also_label)):
        add.append(lid)
    _modify(user_id, message_ids, add=add, remove=[])


def hold(user_id: str, message_ids: list[str]) -> None:
    """Remove messages from the inbox and add the holding label."""
    hold_id = resolve_label_id(user_id, HOLD_LABEL_NAME)
    add = [hold_id] if hold_id else []
    _modify(user_id, message_ids, add=add, remove=[INBOX_LABEL])


def release(user_id: str, message_ids: list[str]) -> None:
    """Return held messages to the inbox and drop the holding label."""
    hold_id = resolve_label_id(user_id, HOLD_LABEL_NAME)
    remove = [hold_id] if hold_id else []
    _modify(user_id, message_ids, add=[INBOX_LABEL], remove=remove)


def list_held_messages(user_id: str, max_results: int = 100) -> list[dict]:
    """Fetch messages currently held (labeled `inboxos-later`, not in inbox)."""
    resp = get_composio().tools.execute(
        FETCH_EMAILS,
        {"query": f"label:{HOLD_LABEL_NAME}", "max_results": max_results},
        user_id=user_id,
    )
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {FETCH_EMAILS} failed: {resp.get('error')}")
    return (resp.get("data") or {}).get("messages") or []


def held_message_ids(user_id: str, max_results: int = 100) -> list[str]:
    return [
        m.get("messageId")
        for m in list_held_messages(user_id, max_results)
        if m.get("messageId")
    ]
