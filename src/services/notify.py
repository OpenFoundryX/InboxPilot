"""Send assistant-originated email that actually lands in the user's inbox.

`GMAIL_SEND_EMAIL` self-sends land in Sent only (no INBOX label), so a briefing
or reminder sent that way is invisible in the inbox. This helper sends and then
forces the message into the inbox, and labels it `inboxos-chat` so the command
sweep never treats our own outgoing mail as a new command.
"""

from core.logging import get_logger
from integrations.composio import gmail
from services.mailman import gmail_ops

log = get_logger(__name__)

CHAT_LABEL = "inboxos-chat"


def send_to_inbox(user_id: str, to: str, subject: str, body: str) -> str | None:
    mid = gmail.send_email(user_id, to, subject, body)
    if mid:
        try:
            gmail_ops.deliver_to_inbox(user_id, [mid], also_label=CHAT_LABEL)
        except Exception:
            log.warning("notify.deliver_failed", user_id=user_id, exc_info=True)
    return mid
