"""Celery task: run one command the user emailed themselves, then reply.

Driven by the Gmail trigger rather than a sweep, which removes the protection
the sweep's `-label:"inboxos-chat"` query used to give us: our own reply is a new
self-addressed message that matches the trigger too. Two independent guards stop
that loop — the Redis registry checked at the webhook door, and the chat-label
check below for anything the registry missed.

Deliberately NOT retried. A failed action is reported to the user inside the
reply (the behaviour the sweep had); retrying a task that already sent mail
would double-send.
"""

import uuid

from core.database import run_async, with_worker_session
from core.idempotency import allow_reply, remember_ours
from core.logging import get_logger
from integrations.google import gmail
from models.users import User
from services.commands import handlers
from services.commands.ask import answer_question
from services.commands.chat import compose_reply
from services.commands.parser import parse_command
from services.commands.render import to_html
from services.mailman import gmail_ops
from services.mailman.store import get_or_create_settings
from services.notify import CHAT_LABEL
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="commands.handle_email")
def handle_command_email(
    user_id: str,
    message_id: str,
    subject: str | None = None,
    body: str | None = None,
    thread_id: str | None = None,
    label_ids: list[str] | None = None,
) -> dict:
    return run_async(
        with_worker_session(
            lambda db: _handle(
                db,
                user_id,
                message_id=message_id,
                subject=subject,
                body=body,
                thread_id=thread_id,
                label_ids=label_ids or [],
            )
        )
    )


async def _handle(
    db,
    user_id: str,
    *,
    message_id: str,
    subject: str | None,
    body: str | None,
    thread_id: str | None,
    label_ids: list[str],
) -> dict:
    uid = uuid.UUID(user_id)
    user = await db.get(User, uid)
    if not user or not user.email:
        return {"skipped": "unknown_user"}

    # Loop guard: anything already filed under the chat label is our own mail.
    # Payload label_ids are Gmail label *ids*, so resolve the name to an id.
    chat_label_id = gmail_ops.resolve_label_id(user_id, CHAT_LABEL)
    if chat_label_id and chat_label_id in label_ids:
        log.info("commands.skipped_own", user_id=user_id, message_id=message_id)
        return {"skipped": "own_message"}

    settings_row = await get_or_create_settings(db, uid)
    plan = parse_command(subject, body, tz=settings_row.timezone)
    actions = plan.get("actions") or []

    results: list[str] = []
    for action in actions:
        try:
            results.append(await handlers.execute(db, uid, action))
        except Exception as ex:
            results.append(f"⚠️ failed: {ex}")
            log.exception("commands.action_failed", user_id=user_id, action=action)
    await db.commit()

    replied = _reply(user_id, user.email, subject, body, thread_id, results, bool(actions))

    try:
        gmail_ops.add_label(user_id, [message_id], CHAT_LABEL)
    except Exception:
        log.exception("commands.file_failed", user_id=user_id, message_id=message_id)

    log.info("commands.processed", user_id=user_id, message_id=message_id, actions=len(actions))
    return {"actions": len(actions), "replied": replied}


def _reply(
    user_id: str,
    email: str,
    subject: str | None,
    body: str | None,
    thread_id: str | None,
    results: list[str],
    had_actions: bool,
) -> bool:
    """Confirm what was done, or answer the question. Returns whether we sent."""
    if thread_id and not allow_reply(thread_id):
        log.error("commands.reply_cap_reached", user_id=user_id, thread_id=thread_id)
        return False

    try:
        if had_actions:
            text = compose_reply(subject, body, results)
        else:
            text = answer_question(user_id, subject, body, account_email=email)
    except Exception:
        log.exception("commands.compose_failed", user_id=user_id)
        return False

    body_html = to_html(text)
    try:
        if thread_id:
            reply_id = gmail.reply_in_thread(user_id, thread_id, email, body_html, is_html=True)
        else:
            reply_id = gmail.send_email(user_id, email, "Re: (your note)", body_html, is_html=True)
    except Exception:
        log.exception("commands.reply_failed", user_id=user_id)
        return False

    if not reply_id:
        return False

    # Register before labelling: the trigger can see our reply within the poll
    # interval, long before Gmail's label change is visible to it.
    remember_ours(reply_id)
    try:
        gmail_ops.add_label(user_id, [reply_id], CHAT_LABEL)
    except Exception:
        log.warning("commands.reply_label_failed", user_id=user_id, exc_info=True)
    return True
