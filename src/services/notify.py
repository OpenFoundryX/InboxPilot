"""Send assistant-originated email under the InboxOS identity.

The assistant sends from the user's `+inboxos` alias (e.g. you+inboxos@gmail.com)
so the message arrives as genuinely *received* mail — it lands in the inbox
naturally, with a clear "InboxOS" sender, and is distinguishable from the user's
own mail. We also label it inboxos-chat so the command sweep never treats our own
outgoing mail as a new command.
"""

from core.idempotency import remember_ours
from core.logging import get_logger
from integrations.google import gmail
from services.mailman import gmail_ops

log = get_logger(__name__)

CHAT_LABEL = "inboxos-chat"


def inboxos_alias(email: str) -> str:
    """you@gmail.com -> you+inboxos@gmail.com (falls back to the plain address)."""
    if "@" not in email or "+" in email.split("@", 1)[0]:
        return email
    local, domain = email.split("@", 1)
    return f"{local}+inboxos@{domain}"


def send_to_inbox(user_id: str, to: str, subject: str, body: str) -> str | None:
    """Send under the InboxOS identity (+inboxos alias) into the user's inbox.

    The alias gives the message a clear sender; forcing INBOX makes delivery
    reliable (Gmail's INBOX label on self-alias sends is delayed/inconsistent).
    Also labels inboxos-chat so the command sweep ignores our own mail.
    """
    mid = gmail.send_email(user_id, to, subject, body, from_email=inboxos_alias(to))
    if mid:
        # Register before anything else: this message matches the Gmail trigger,
        # and without the marker it comes back to us as a brand-new command.
        remember_ours(mid)
        try:
            gmail_ops.deliver_to_inbox(user_id, [mid], also_label=CHAT_LABEL)
        except Exception:
            log.warning("notify.deliver_failed", user_id=user_id, exc_info=True)
    return mid
