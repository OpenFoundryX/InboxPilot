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
    # .link() is the current endpoint for Composio-managed OAuth auth configs;
    # .initiate() was retired for those (POST /api/v3/connected_accounts/link).
    request = get_composio().connected_accounts.link(
        user_id=user_id,
        auth_config_id=settings.COMPOSIO_GMAIL_AUTH_CONFIG_ID,
        callback_url=callback_url or settings.COMPOSIO_GMAIL_CALLBACK_URL,
    )
    return request.redirect_url


def fetch_recent_emails(user_id: str, days: int = 7, max_results: int = 25) -> list[EmailSummary]:
    """Fetch the user's emails from the last `days` days.

    Uses Gmail's `newer_than:Nd` search operator. Requires an ACTIVE connection.
    """
    resp = get_composio().tools.execute(
        FETCH_EMAILS,
        {
            "query": f"newer_than:{days}d",
            "max_results": max_results,
        },
        user_id=user_id,
    )

    # The SDK returns a plain dict: {"data": {...}, "error": ..., "successful": bool}.
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {FETCH_EMAILS} failed: {resp.get('error')}")

    messages = (resp.get("data") or {}).get("messages") or []
    return [_summarize(m) for m in messages]


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
