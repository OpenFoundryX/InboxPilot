"""Gmail integration via Composio.

Composio manages its own OAuth grant to the user's Gmail (separate from this
app's Google login). A user must have an ACTIVE Composio Gmail connection
before emails can be fetched.

Every function takes `user_id` — the Composio entity id. We use the app user's
UUID (as a string) for this, so one app user maps to one Composio entity.

Note: these call the Composio SDK, which is synchronous and does blocking HTTP.
Call them from Celery tasks directly, or from async code via a threadpool.
"""

from typing import Any

from core.config import settings
from integrations.composio.composio_client import get_composio
from schemas.email import EmailSummary

GMAIL_TOOLKIT = "gmail"
FETCH_EMAILS = "GMAIL_FETCH_EMAILS"
LIST_LABELS = "GMAIL_LIST_LABELS"
CREATE_LABEL = "GMAIL_CREATE_LABEL"
DELETE_LABEL = "GMAIL_DELETE_LABEL"
SEND_EMAIL = "GMAIL_SEND_EMAIL"
REPLY_TO_THREAD = "GMAIL_REPLY_TO_THREAD"
CREATE_DRAFT = "GMAIL_CREATE_EMAIL_DRAFT"


INBOXPILOT_LABELS: dict[str, dict[str, str]] = {

    "to do": {"background_color": "#fb4c2f", "text_color": "#ffffff"},  # red
    "notification": {"background_color": "#4a86e8", "text_color": "#ffffff"},  # blue
    "fyi": {"background_color": "#16a766", "text_color": "#ffffff"},  # green
    "marketing": {"background_color": "#fad165", "text_color": "#000000"},  # yellow
    "noise": {"background_color": "#999999", "text_color": "#ffffff"},  # grey
    "to follow up": {"background_color": "#a479e2", "text_color": "#ffffff"},  # purple

    "inboxos-chat": {"background_color": "#2da2bb", "text_color": "#ffffff", "label_list_visibility": "labelShowIfUnread"},  # teal
    "inboxos-routines": {"background_color": "#ffad47", "text_color": "#000000", "label_list_visibility": "labelShowIfUnread"},  # orange
    "inboxos-later": {"background_color": "#f691b3", "text_color": "#000000", "label_list_visibility": "labelShowIfUnread"},  # pink
    "inboxos-rules": {"background_color": "#efa093", "text_color": "#000000", "label_list_visibility": "labelShowIfUnread"},  # salmon
}


def create_label(user_id: str, name: str) -> str:
    """Create an arbitrary Gmail label by name; return its id.

    Idempotent-ish: if a label with this name already exists, returns that id
    instead of failing on Gmail's duplicate-name 409.
    """

    existing = _find_label_id(user_id, name)
    if existing:
        return existing

    resp = get_composio().tools.execute(
        CREATE_LABEL, 
        {
            "label_name": name
        }, 
        user_id=user_id
    )

    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {CREATE_LABEL} failed for {name!r}: {resp.get('error')}")

    data = resp.get("data")
    return data.get("id") or data.get("response_data").get("id")


def delete_label(user_id: str, name: str) -> bool:
    """Delete a Gmail label by name. Returns True if it existed and was removed."""

    label_id = _find_label_id(user_id, name)
    if not label_id:
        return False
    
    resp = get_composio().tools.execute(
        DELETE_LABEL, 
        {
            "label_id": label_id
        }, 
        user_id=user_id
    )

    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {DELETE_LABEL} failed for {name!r}: {resp.get('error')}")
    return True


def _find_label_id(user_id: str, name: str) -> str | None:
    resp = get_composio().tools.execute(LIST_LABELS, {}, user_id=user_id)
    if resp.get("successful") is False:
        return None
    for label in (resp.get("data") or {}).get("labels") or []:
        if label.get("name").casefold() == name.casefold():
            return label.get("id")
    return None


def send_email(
    user_id: str, 
    to: str, 
    subject: str, 
    body: str, 
    from_email: str | None = None
) -> str | None:
    """Send a plain-text email; return the sent message id if available.

    When `from_email` is a +alias of the account (e.g. you+inboxos@gmail.com),
    the message is delivered to the inbox as genuinely *received* mail with that
    sender identity — no need to force the INBOX label afterwards.
    """

    payload: dict = {
        "recipient_email": to,
        "subject": subject,
        "body": body,
        "is_html": False,
    }

    if from_email:
        payload["from_email"] = from_email
    resp = get_composio().tools.execute(SEND_EMAIL, payload, user_id=user_id)
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {SEND_EMAIL} failed: {resp.get('error')}")
    data = resp.get("data")
    return data.get("id") or data.get("response_data").get("id")


def create_draft(
    user_id: str, to: str, subject: str, body: str, thread_id: str | None = None
) -> str | None:
    """Create a draft reply (threaded when thread_id is given). Never sends."""
    payload: dict = {"recipient_email": to, "subject": subject, "body": body, "is_html": False}
    if thread_id:
        payload["thread_id"] = thread_id
    resp = get_composio().tools.execute(CREATE_DRAFT, payload, user_id=user_id)
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {CREATE_DRAFT} failed: {resp.get('error')}")
    data = resp.get("data")
    return data.get("id") or data.get("response_data").get("id")


def reply_in_thread(user_id: str, thread_id: str, to: str, body: str) -> str | None:
    """Reply within an existing thread (keeps the conversation threaded)."""
    resp = get_composio().tools.execute(
        REPLY_TO_THREAD,
        {
            "thread_id": thread_id,
            "recipient_email": to,
            "message_body": body,
            "is_html": False,
        },
        user_id=user_id,
    )
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {REPLY_TO_THREAD} failed: {resp.get('error')}")
    data = resp.get("data")
    return data.get("id") or data.get("response_data").get("id")


def get_active_connection(user_id: str) -> Any | None:
    """Return the user's ACTIVE Gmail connection, or None if not connected."""
    res = get_composio().connected_accounts.list(
        user_ids=[user_id],
        toolkit_slugs=[GMAIL_TOOLKIT],
        statuses=["ACTIVE"],
    )
    items = getattr(res, "items", None) or []
    return items[0] if items else None


def is_connected(user_id: str) -> bool:
    return get_active_connection(user_id) is not None


def initiate_connection(user_id: str, callback_url: str | None = None) -> str:
    """Start the Gmail OAuth grant. Returns a redirect URL to send the user to."""
    if not settings.COMPOSIO_GMAIL_AUTH_CONFIG_ID:
        raise RuntimeError("COMPOSIO_GMAIL_AUTH_CONFIG_ID is not configured")

    request = get_composio().connected_accounts.link(
        user_id=user_id,
        auth_config_id=settings.COMPOSIO_GMAIL_AUTH_CONFIG_ID,
        callback_url=callback_url or settings.COMPOSIO_GMAIL_CALLBACK_URL,
    )
    return request.redirect_url


def ensure_labels(user_id: str) -> list[str]:
    """Ensure InboxPilot's organizational labels exist in the user's Gmail.

    Idempotent: lists the account's existing labels and creates only the ones
    that are missing (case-insensitive match, since Gmail rejects duplicate
    names). Returns the names that were newly created (empty on later runs).

    Blocking Composio calls — invoke from a Celery task or a threadpool.
    """
    client = get_composio()

    resp = client.tools.execute(LIST_LABELS, {}, user_id=user_id)
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {LIST_LABELS} failed: {resp.get('error')}")

    existing = {
        (label.get("name") or "").casefold()
        for label in (resp.get("data") or {}).get("labels") or []
    }

    created: list[str] = []
    for name, colors in INBOXPILOT_LABELS.items():
        if name.casefold() in existing:
            continue
        res = client.tools.execute(
            CREATE_LABEL, {"label_name": name, **colors}, user_id=user_id
        )
        if res.get("successful") is False:
            raise RuntimeError(f"Composio {CREATE_LABEL} failed for {name!r}: {res.get('error')}")
        created.append(name)

    return created


def fetch_by_query(user_id: str, query: str, max_results: int = 25) -> list[EmailSummary]:
    """Fetch emails matching a Gmail search query."""
    resp = get_composio().tools.execute(
        FETCH_EMAILS,
        {"query": query, "max_results": max_results},
        user_id=user_id,
    )

    # The SDK returns a plain dict: {"data": {...}, "error": ..., "successful": bool}.
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {FETCH_EMAILS} failed: {resp.get('error')}")

    messages = (resp.get("data") or {}).get("messages") or []
    return [_summarize(m) for m in messages]


def fetch_recent_emails(user_id: str, days: int = 7, max_results: int = 25) -> list[EmailSummary]:
    """Fetch the user's emails from the last `days` days.

    Uses Gmail's `newer_than:Nd` search operator. Requires an ACTIVE connection.
    """
    return fetch_by_query(user_id, f"newer_than:{days}d", max_results)


def _summarize(m: dict) -> EmailSummary:
    """Map a Gmail message (Composio shape) to an EmailSummary."""
    preview = m.get("preview")
    snippet = preview.get("body") if isinstance(preview, dict) else preview
    if not snippet:
        snippet = m.get("messageText")
    if isinstance(snippet, str):
        snippet = snippet.strip()[:200]
    return EmailSummary(
        id=m.get("messageId"),
        thread_id=m.get("threadId"),
        sender=m.get("sender"),
        to=m.get("to"),
        subject=m.get("subject"),
        snippet=snippet,
        body=m.get("messageText"),
        date=m.get("messageTimestamp"),
        labels=m.get("labelIds") or [],
    )
