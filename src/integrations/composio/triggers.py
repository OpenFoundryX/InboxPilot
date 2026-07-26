"""Composio trigger + webhook subscription helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from core.config import settings
from core.logging import get_logger
from integrations.composio.composio_client import get_composio

log = get_logger(__name__)

GMAIL_NEW_MESSAGE = "GMAIL_NEW_GMAIL_MESSAGE"

# Mailman's hold filter strips INBOX at delivery, so watching INBOX alone would
# miss every held message — exactly the mail that most needs classifying before
# its batch is released. `query` takes precedence over `labelIds` in Composio's
# trigger config. The label name is quoted because of the hyphen.
GMAIL_TRIGGER_QUERY = 'label:inbox OR label:"inboxos-later"'
# Minutes between Composio's polls of the user's mailbox.
GMAIL_TRIGGER_INTERVAL = 1


def webhook_url() -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_PREFIX}/webhooks/composio"


def gmail_oauth_callback_url(user_id: str) -> str:
    """OAuth return URL — our own route, which starts onboarding.

    Composio sends the user's browser here after the grant and appends `status`
    and `connected_account_id`. It has to be publicly reachable, hence
    PUBLIC_BASE_URL rather than a localhost address.
    """
    base = f"{settings.PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_PREFIX}"
    return f"{base}/integrations/gmail/callback?{urlencode({'user_id': user_id})}"


def ensure_webhook_subscription() -> None:
    """Point the project webhook at our receiver. Safe on every API startup."""

    sub = get_composio().triggers.set_webhook_subscription(webhook_url=webhook_url())
    log.info("composio.webhook_subscribed", webhook_url=sub.get("webhook_url"), subscription_id=sub.get("id"))


def _matching_gmail_trigger(user_id: str) -> str | None:
    """An existing trigger for this user already watching the right query."""
    res = get_composio().triggers.list_active(trigger_names=[GMAIL_NEW_MESSAGE])
    stale: list[str] = []
    for item in getattr(res, "items", None) or []:
        if getattr(item, "user_id", None) != user_id:
            continue
        config = getattr(item, "trigger_config", None) or {}
        if config.get("query") == GMAIL_TRIGGER_QUERY:
            return getattr(item, "id", None)
        stale.append(getattr(item, "id", None))
    if stale:
        # An INBOX-only trigger cannot see held mail, so we still create a
        # correct one. Both will fire until the stale instance is removed;
        # duplicate events are absorbed by the webhook's dedupe guard.
        log.warning("composio.gmail_trigger_stale", user_id=user_id, trigger_ids=stale)
    return None


def ensure_gmail_new_message_trigger(user_id: str, connected_account_id: str | None = None) -> str:
    """Ensure the new-mail trigger exists for a connected Gmail account.

    `triggers.create` does NOT upsert despite its response type's name — calling
    it repeatedly creates additional instances. Since re-running the onboarding
    sync is the supported catch-up lever, an existing match is reused.
    """
    if existing := _matching_gmail_trigger(user_id):
        log.info("composio.gmail_trigger_reused", user_id=user_id, trigger_id=existing)
        return existing

    trigger = get_composio().triggers.create(
        slug=GMAIL_NEW_MESSAGE,
        user_id=user_id,
        connected_account_id=connected_account_id,
        trigger_config={
            "query": GMAIL_TRIGGER_QUERY,
            "interval": GMAIL_TRIGGER_INTERVAL,
            "userId": "me",
        },
    )
    # The SDK returns a TriggerInstanceUpsertResponse model — not a mapping —
    # and its id field is `trigger_id`.
    trigger_id = trigger.trigger_id
    log.info(
        "composio.gmail_trigger_ready",
        user_id=user_id,
        trigger_id=trigger_id,
        connected_account_id=connected_account_id,
    )
    return trigger_id


def has_gmail_trigger(connected_account_id: str) -> bool:
    """Whether new-mail events will actually reach us for this connection.

    Worth surfacing: with no sweeps left, a user whose trigger was deleted or
    disabled goes completely silent and nothing else would notice.
    """
    res = get_composio().triggers.list_active(
        connected_account_ids=[connected_account_id],
        trigger_names=[GMAIL_NEW_MESSAGE],
    )
    return bool(getattr(res, "items", None))


def parse_webhook(body: bytes, headers: Any) -> dict[str, Any]:
    """Verify signature and normalize a Composio webhook payload."""

    secret = settings.COMPOSIO_WEBHOOK_SECRET
    return get_composio().triggers.parse(
        body=body, 
        headers=headers, 
        verify_secret=secret
    )
