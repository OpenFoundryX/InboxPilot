"""Verifying and decoding Gmail's Pub/Sub push notifications.

Gmail publishes mailbox changes to a Pub/Sub topic; Pub/Sub POSTs them here.
The envelope is Pub/Sub's, the inner message is Gmail's, and neither says what
actually changed — the payload is only::

    {"emailAddress": "someone@example.com", "historyId": "9876543210"}

which is why the history walk in `integrations.google.gmail` remains the thing
that finds new mail. Push replaces the *timer*, not the lookup.
"""

import base64
import binascii
import json
from dataclasses import dataclass

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)

GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})


class InvalidPushNotification(ValueError):
    """The request did not come from our Pub/Sub subscription, or was malformed."""


@dataclass(frozen=True)
class MailboxNotification:
    """Gmail's "something changed in this mailbox" ping."""

    email: str
    # Gmail's history id at publish time. Deliberately NOT used as the cursor to
    # walk from — see `parse` for why.
    history_id: str | None


def verify_push_token(authorization: str | None) -> None:
    """Check the OIDC token Pub/Sub attaches to a push request.

    Skipped entirely when no service account is configured, matching how the
    rest of this codebase treats webhook secrets: blank disables the check, and
    the setting documents that production must set it.

    Worth being clear about the actual exposure, because it is narrower than it
    looks: this endpoint takes no data from the caller beyond a mailbox address,
    and responds by reading Gmail through *our own* stored grant. A forged
    request cannot inject mail or read anything back — it can only make the app
    spend Gmail quota on a mailbox we already have access to.
    """
    expected_sa = settings.GOOGLE_PUBSUB_SA_EMAIL
    if not expected_sa:
        return

    if not authorization or not authorization.lower().startswith("bearer "):
        raise InvalidPushNotification("missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            settings.GOOGLE_PUBSUB_AUDIENCE or None,
        )
    except ValueError as exc:
        raise InvalidPushNotification(f"token verification failed: {exc}") from exc

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise InvalidPushNotification("unexpected issuer")

    if claims.get("email") != expected_sa:
        raise InvalidPushNotification("unexpected service account")

    if not claims.get("email_verified", False):
        raise InvalidPushNotification("service account email not verified")


def parse(body: bytes) -> MailboxNotification:
    """Unwrap the Pub/Sub envelope and read Gmail's payload out of it.

    The `historyId` in here is reported but never used as the starting cursor.
    Pub/Sub does not guarantee ordering, so a delayed notification can carry an
    id *older* than the one already stored — walking from it would replay mail,
    and trusting a newer one would skip whatever fell between. The stored cursor
    is the only safe place to resume from, and the notification's job is simply
    to say "look now".
    """
    try:
        envelope = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidPushNotification("body is not JSON") from exc

    if not isinstance(envelope, dict):
        raise InvalidPushNotification("envelope is not an object")

    message = envelope.get("message")
    if not isinstance(message, dict):
        raise InvalidPushNotification("envelope has no message")

    encoded = message.get("data")
    if not encoded:
        raise InvalidPushNotification("message has no data")

    try:
        raw = base64.b64decode(encoded)
        payload = json.loads(raw)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise InvalidPushNotification("message data is not base64 JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidPushNotification("message data is not an object")

    email = payload.get("emailAddress")
    if not email:
        raise InvalidPushNotification("payload has no emailAddress")

    history_id = payload.get("historyId")
    return MailboxNotification(
        email=str(email),
        history_id=str(history_id) if history_id is not None else None,
    )
