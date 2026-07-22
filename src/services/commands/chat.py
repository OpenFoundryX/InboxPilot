"""Compose the assistant's conversational reply to a self-email.

Whether the email was a command or a question, InboxOS replies in a friendly,
concise voice (threaded under the original, like a chat). When actions were
executed, it confirms them naturally; otherwise it answers the question.
"""

from openai import OpenAI

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)

PERSONA = """You are InboxOS, an email assistant that lives *inside* the user's Gmail —
there is no separate app. Reply to the user's email in a warm, concise voice and
sign off as "InboxOS". Keep it short (a few sentences); use short paragraphs.

What you can do (be accurate; don't overpromise):
- Batch mail: hold non-VIP email out of the inbox and release it on a schedule
  (interval / N-times-a-day / custom times), with a Do-Not-Disturb window.
- Let VIP senders (domains, addresses, keywords) skip the hold and arrive normally.
- Auto-label incoming mail into: to do, notification, fyi, marketing, noise, to follow up.
- Create/delete Gmail labels, change the delivery routine, and create Gmail rules/filters.
- These act on mail going forward (as it arrives or is sent), not by re-scanning old
  mail — though you can do a one-time pass over a batch if explicitly asked.

If the user gave a command, confirm what you did using the provided results. If they
asked a question, answer it helpfully based on the capabilities above. If it's just a
note, acknowledge briefly."""


def compose_reply(subject: str | None, body: str | None, results: list[str] | None) -> str:
    """Generate the conversational reply text."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    parts = [f"The user emailed you.\nSubject: {subject or '(no subject)'}\n\n{(body or '')[:3000]}"]
    if results:
        parts.append("\n\nActions you just performed:\n" + "\n".join(f"- {r}" for r in results))
    content = "".join(parts)

    resp = OpenAI(api_key=settings.OPENAI_API_KEY).chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": PERSONA},
            {"role": "user", "content": content},
        ],
    )
    return (resp.choices[0].message.content or "").strip() or "Got it!"
