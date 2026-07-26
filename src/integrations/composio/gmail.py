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

from composio_client import APIStatusError

from core.config import settings
from core.logging import get_logger
from integrations.composio.composio_client import get_composio
from schemas.email import EmailSummary

log = get_logger(__name__)

GMAIL_TOOLKIT = "gmail"
FETCH_EMAILS = "GMAIL_FETCH_EMAILS"
LIST_LABELS = "GMAIL_LIST_LABELS"
CREATE_LABEL = "GMAIL_CREATE_LABEL"
DELETE_LABEL = "GMAIL_DELETE_LABEL"
SEND_EMAIL = "GMAIL_SEND_EMAIL"
REPLY_TO_THREAD = "GMAIL_REPLY_TO_THREAD"
CREATE_DRAFT = "GMAIL_CREATE_EMAIL_DRAFT"

# Per-request page size. Full bodies (verbose=true) easily hit Composio's 413.
FETCH_PAGE = 25
# Hard ceiling when fetching "all" so a huge mailbox can't run forever.
FETCH_ALL_CAP = 2000

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


def _find_label_id(user_id: str, name: str) -> str | None:
    resp = get_composio().tools.execute(LIST_LABELS, {}, user_id=user_id)
    if resp.get("successful") is False:
        return None
    for label in resp.get("data").get("labels") or []:
        if label.get("name").casefold() == name.casefold():
            return label.get("id")
    return None


def create_label(user_id: str, name: str) -> str:
    """Create an arbitrary Gmail label by name; return its id.

    Idempotent-ish: if a label with this name already exists, returns that id
    instead of failing on Gmail's duplicate-name 409.
    """

    resp = get_composio().tools.execute(
        CREATE_LABEL, 
        {
            "label_name": name
        }, 
        user_id=user_id
    )

    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {CREATE_LABEL} failed for {name!r}: {resp.get('error')}")

    return resp.get("data").get("id")


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


def send_email(
    user_id: str, 
    to: str,
    subject: str,
    body: str,
    from_email: str | None = None,
    is_html: bool = False,
) -> str | None:
    """Send an email; return the sent message id if available.

    When `from_email` is a +alias of the account (e.g. you+inboxos@gmail.com),
    the message is delivered to the inbox as genuinely *received* mail with that
    sender identity — no need to force the INBOX label afterwards.
    """

    payload: dict = {
        "recipient_email": to,
        "subject": subject,
        "body": body,
        "is_html": is_html,
    }

    if from_email:
        payload["from_email"] = from_email
    resp = get_composio().tools.execute(SEND_EMAIL, payload, user_id=user_id)
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {SEND_EMAIL} failed: {resp.get('error')}")

    return resp.get("data").get("id")


def create_draft(user_id: str, to: str, subject: str, body: str, thread_id: str | None = None) -> str | None:
    """
        Create a draft reply (threaded when thread_id is given). Never sends.
    """

    payload: dict = {"recipient_email": to, "subject": subject, "body": body, "is_html": False}
    if thread_id:
        payload["thread_id"] = thread_id

    resp = get_composio().tools.execute(CREATE_DRAFT, payload, user_id=user_id)
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {CREATE_DRAFT} failed: {resp.get('error')}")

    return resp.get("data").get("id")


def reply_in_thread(user_id: str, thread_id: str, to: str, body: str, is_html: bool = False) -> str | None:
    """Reply within an existing thread (keeps the conversation threaded)."""

    resp = get_composio().tools.execute(
        REPLY_TO_THREAD,
        {
            "thread_id": thread_id,
            "recipient_email": to,
            "message_body": body,
            "is_html": is_html,
        },
        user_id=user_id,
    )

    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {REPLY_TO_THREAD} failed: {resp.get('error')}")

    return resp.get("data").get("id")


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

    from integrations.composio.triggers import gmail_oauth_callback_url

    request = get_composio().connected_accounts.link(
        user_id=user_id,
        auth_config_id=settings.COMPOSIO_GMAIL_AUTH_CONFIG_ID,
        callback_url=callback_url or gmail_oauth_callback_url(user_id),
    )
    return request.redirect_url


def ensure_labels(user_id: str) -> list[str]:
    """
    Ensure InboxPilot's organizational labels exist in the user's Gmail.

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
        label.get("name").casefold() for label in resp.get("data").get("labels")
    }

    created: list[str] = []
    for name, colors in INBOXPILOT_LABELS.items():
        if name.casefold() in existing:
            continue
    
        res = client.tools.execute(
            CREATE_LABEL, 
            {"label_name": name, **colors}, 
            user_id=user_id
        )
    
        if res.get("successful") is False:
            raise RuntimeError(f"Composio {CREATE_LABEL} failed for {name!r}: {res.get('error')}")
        created.append(name)

    return created


def fetch_by_query(
    user_id: str,
    query: str,
    max_results: int | None = 25,
    *,
    verbose: bool = False,
) -> list[EmailSummary]:
    """Fetch emails matching a Gmail search query.

    Pages through `GMAIL_FETCH_EMAILS` via `nextPageToken` until it has
    `max_results` messages, or — when `max_results` is None — until the query
    is exhausted (capped at `FETCH_ALL_CAP`).

    Defaults to metadata-only (`verbose=False`) to stay under Composio's
    response size limit. Pass `verbose=True` when you need full bodies.
    """
    client = get_composio()
    out: list[EmailSummary] = []
    token: str | None = None
    limit = FETCH_ALL_CAP if max_results is None else max_results
    page = FETCH_PAGE

    while len(out) < limit:
        want = min(page, limit - len(out))
        payload: dict = {
            "query": query,
            "max_results": want,
            "verbose": verbose,
            "include_payload": verbose,
        }
        if token:
            payload["page_token"] = token
        try:
            resp = client.tools.execute(FETCH_EMAILS, payload, user_id=user_id)
        except APIStatusError as exc:
            if getattr(exc, "status_code", None) == 413 and want > 1:
                page = max(1, want // 2)
                log.warning(
                    "gmail.fetch_payload_too_large",
                    user_id=user_id,
                    retry_page=page,
                )
                continue
            raise
    
        if resp.get("successful") is False:
            raise RuntimeError(f"Composio {FETCH_EMAILS} failed: {resp.get('error')}")

        data = resp.get("data") or {}
        batch = data.get("messages") or []
        out.extend(_summarize(m) for m in batch)
        token = data.get("nextPageToken") or data.get("next_page_token")
        if not token or not batch:
            break

    if max_results is None and token:
        log.warning("gmail.fetch_all_capped", user_id=user_id, fetched=len(out), cap=FETCH_ALL_CAP)

    return out[:limit]


def fetch_recent_emails(
    user_id: str,
    days: int = 30,
    max_results: int | None = None,
) -> list[EmailSummary]:
    """Fetch the user's emails from the last `days` days.

    By default pages until the window is exhausted (up to `FETCH_ALL_CAP`).
    Pass `max_results` to stop earlier. Uses Gmail's `newer_than:Nd` operator.
    """
    return fetch_by_query(user_id, f"newer_than:{days}d", max_results)


def _summarize(m: dict) -> EmailSummary:
    """Map a Gmail message (Composio shape) to an EmailSummary."""

    preview = m.get("preview")
    snippet = preview.get("body")

    if not snippet:
        snippet = m.get("messageText")

    attachments = [
        a.get("filename") for a in (m.get("attachmentList")) if a.get("filename")
    ]

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
        attachments=attachments,
    )
