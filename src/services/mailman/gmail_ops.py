"""Gmail label operations for holding/releasing mail (Composio, blocking).

Holding a message = pull it out of the inbox by removing the INBOX label and
adding our holding label (`inboxos-later`). Releasing = the reverse. Call these
from Celery tasks or a threadpool.
"""

from functools import lru_cache

from integrations.composio.composio_client import get_composio
from integrations.composio.gmail import FETCH_EMAILS, LIST_LABELS

BATCH_MODIFY = "GMAIL_BATCH_MODIFY_MESSAGES"
INBOX_LABEL = "INBOX"
HOLD_LABEL_NAME = "inboxos-later"


@lru_cache(maxsize=256)
def resolve_label_id(user_id: str, name: str) -> str | None:
    """Resolve a Gmail label name to its id for a user (cached per process)."""
    resp = get_composio().tools.execute(LIST_LABELS, {}, user_id=user_id)
    if resp.get("successful") is False:
        return None
    for label in (resp.get("data") or {}).get("labels") or []:
        if (label.get("name") or "").casefold() == name.casefold():
            return label.get("id")
    return None


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
