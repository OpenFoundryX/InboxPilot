"""Celery beat job: auto-label recently-arrived email into the org labels.

Every ~minute, for each connected Gmail account, fetch recent messages that
don't yet carry one of the six org labels, classify each with the LLM, and apply
the chosen label. Idempotent: once labeled, a message no longer matches the
"unlabeled" query, so it won't be re-classified.
"""

from integrations.composio import gmail
from integrations.composio.composio_client import get_composio
from core.logging import get_logger
from services.classify.classifier import LABEL_NAMES, classify
from services.mailman import gmail_ops
from workers.celery_app import celery_app

log = get_logger(__name__)

# How far back / how many to consider each sweep (cost + latency guardrails).
LOOKBACK = "newer_than:2d"
MAX_PER_SWEEP = 15


def _unlabeled_query() -> str:
    """Recent *received* mail carrying none of the org labels.

    Excludes `from:me` so the user's own sent mail and their InboxOS chat/command
    threads (which get the `inboxos-chat` label) are never also given an org
    label — every email ends up with at most one of our labels.
    """
    negations = " ".join(f'-label:"{name}"' for name in LABEL_NAMES)
    return f"{LOOKBACK} -from:me {negations}"


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


@celery_app.task(name="classify.sweep")
def sweep() -> dict:
    labeled = 0
    for user_id in _active_gmail_user_ids():
        try:
            labeled += _sweep_user(user_id)
        except Exception:
            log.exception("classify.sweep_user_failed", user_id=user_id)
    return {"labeled": labeled}


def _sweep_user(user_id: str) -> int:
    emails = gmail.fetch_by_query(user_id, _unlabeled_query(), MAX_PER_SWEEP)
    labeled = 0
    for e in emails:
        if not e.id:
            continue
        try:
            label = classify(e.sender, e.subject, e.snippet)
        except Exception:
            log.exception("classify.call_failed", user_id=user_id, message_id=e.id)
            break  # likely a config/quota problem; stop hammering this sweep
        if not label:
            continue
        gmail_ops.add_label(user_id, [e.id], label)
        labeled += 1
        log.info("classify.labeled", user_id=user_id, message_id=e.id, label=label)
    return labeled
