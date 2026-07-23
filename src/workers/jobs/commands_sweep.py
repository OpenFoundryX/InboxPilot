"""Celery beat job: converse with the user via emails they send themselves.

Every ~minute, for each connected Gmail account that maps to a real app user,
fetch unprocessed self-emails, parse each into typed actions with the LLM,
execute them, and reply *in the same thread* with a conversational message. Each
handled message (and our reply) is labeled `inboxos-chat`, which both records the
conversation and prevents reprocessing.
"""

import uuid

from core.config import settings
from core.database import run_async, with_worker_session
from core.locks import single_run
from core.logging import get_logger
from integrations.composio import gmail
from integrations.composio.composio_client import get_composio
from models.users import User
from services.commands import handlers
from services.commands.ask import answer_question
from services.commands.chat import compose_reply
from services.commands.parser import parse_command
from services.commands.render import to_html
from services.mailman import gmail_ops
from services.mailman.store import get_or_create_settings
from workers.celery_app import celery_app

log = get_logger(__name__)

# Conversation label: applied to handled self-emails and our replies. Also the
# processed-marker (excluded from the fetch query so nothing is handled twice).
CHAT_LABEL = "inboxos-chat"
# Legacy marker from the pre-thread version; still excluded so old test commands
# aren't reprocessed.
LEGACY_LABEL = "inboxos-rules"


def _active_gmail_user_ids() -> list[str]:
    res = get_composio().connected_accounts.list(
        toolkit_slugs=["gmail"], statuses=["ACTIVE"]
    )
    seen: list[str] = []
    for acct in getattr(res, "items", []) or []:
        uid = getattr(acct, "user_id", None)
        if uid and uid not in seen:
            seen.append(uid)
    return seen


@celery_app.task(name="commands.sweep")
def sweep() -> dict:
    # Never let two sweeps run at once — they have external side effects
    # (sending mail, creating filters) that must not double-execute.
    with single_run("commands.sweep") as acquired:
        if not acquired:
            return {"skipped": "locked"}
        return run_async(with_worker_session(_sweep))


async def _sweep(db) -> dict:
    processed = 0
    for user_id in _active_gmail_user_ids():
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            continue  # not an app user (e.g. a test entity)
        user = await db.get(User, uid)
        if not user or not user.email:
            continue
        try:
            processed += await _sweep_user(db, uid, user.email)
        except Exception:
            log.exception("commands.sweep_user_failed", user_id=user_id)
    return {"processed": processed}


async def _sweep_user(db, uid: uuid.UUID, email: str) -> int:
    user_id = str(uid)
    # Exclude drafts (half-written commands) and anything already handled
    # (current CHAT_LABEL or the legacy marker).
    query = (
        f'from:me to:me -in:drafts {settings.COMMANDS_LOOKBACK} '
        f'-label:"{CHAT_LABEL}" -label:"{LEGACY_LABEL}"'
    )
    emails = gmail.fetch_by_query(user_id, query, settings.COMMANDS_MAX_PER_SWEEP)

    count = 0
    for e in emails:
        if not e.id:
            continue
        try:
            settings_row = await get_or_create_settings(db, uid)
            plan = parse_command(e.subject, e.body, tz=settings_row.timezone)
        except Exception:
            log.exception("commands.parse_failed", user_id=user_id, message_id=e.id)
            break  # likely config/quota; stop this sweep

        results: list[str] = []
        for action in plan["actions"]:
            try:
                results.append(await handlers.execute(db, uid, action))
            except Exception as ex:
                results.append(f"⚠️ failed: {ex}")
                log.exception("commands.action_failed", user_id=user_id, action=action)
        # Persist DB-side effects (routine/VIP) for this command.
        await db.commit()

        # Always reply, in-thread and conversationally (like a chat assistant).
        # Commands → confirm what was done; questions/notes → answer grounded
        # in the user's inbox ("Ask anything").
        _reply_in_thread(user_id, e, email, results, had_actions=bool(plan["actions"]))

        # File the command so it's never reprocessed.
        try:
            gmail_ops.add_label(user_id, [e.id], CHAT_LABEL)
        except Exception:
            log.exception("commands.file_failed", user_id=user_id, message_id=e.id)

        count += 1
        log.info(
            "commands.processed",
            user_id=user_id,
            message_id=e.id,
            actions=len(plan["actions"]),
        )
    return count


def _reply_in_thread(
    user_id: str, command, email: str, results: list[str], had_actions: bool
) -> None:
    """Compose a reply and send it threaded under the command.

    Commands get a confirmation of what was done; questions/notes get an
    inbox-grounded answer ("Ask anything").
    """
    try:
        if had_actions:
            text = compose_reply(command.subject, command.body, results)
        else:
            text = answer_question(
                user_id, command.subject, command.body, account_email=email
            )
    except Exception:
        log.exception("commands.compose_failed", user_id=user_id)
        return

    # Render the Markdown reply into a clean, inline-styled HTML email.
    body_html = to_html(text)

    try:
        if command.thread_id:
            reply_id = gmail.reply_in_thread(
                user_id, command.thread_id, email, body_html, is_html=True
            )
        else:
            reply_id = gmail.send_email(
                user_id, email, "Re: (your note)", body_html, is_html=True
            )
    except Exception:
        log.exception("commands.reply_failed", user_id=user_id)
        return

    # Label our own reply so it's never picked up as a new command (loop guard).
    if reply_id:
        try:
            gmail_ops.add_label(user_id, [reply_id], CHAT_LABEL)
        except Exception:
            log.warning("commands.reply_label_failed", user_id=user_id, exc_info=True)
