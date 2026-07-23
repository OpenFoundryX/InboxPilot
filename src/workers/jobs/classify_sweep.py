"""Celery beat job: auto-label recently-arrived email into the org labels.

Every ~minute, for each connected Gmail account, fetch recent messages that
don't yet carry one of the six org labels, classify each with the LLM, and apply
the chosen label. Idempotent: once labeled, a message no longer matches the
"unlabeled" query, so it won't be re-classified.
"""

from functools import lru_cache

from integrations.composio import gmail
from integrations.composio.composio_client import get_composio
from core.logging import get_logger
from services.classify.classifier import LABEL_NAMES, classify
from services.mailman import gmail_ops
from workers.celery_app import celery_app

log = get_logger(__name__)


@lru_cache(maxsize=256)
def _ensure_labels_once(user_id: str) -> bool:
    """Provision the org labels for a user, at most once per worker process.

    The classifier can only apply a label that exists in the user's Gmail.
    Accounts that skipped (or failed) the initial sync would otherwise crash the
    sweep with `label '<name>' not found`. This is idempotent and cheap (one
    LIST + creates only what's missing); the lru_cache means it runs once per
    user per process — a raised error is not cached, so it retries next sweep.
    """
    gmail.ensure_labels(user_id)
    return True

# How far back / how many to consider each sweep (cost + latency guardrails).
# Lookback matches the initial sync window (last 7 days) so mail surfaced by a
# sync doesn't fall outside the classifier's reach and stay permanently untagged.
# The per-sweep cap bounds cost/latency; a backlog is chewed down over successive
# minute-ly sweeps (25/min = 1500/hr) until the "unlabeled" query drains to zero.
LOOKBACK = "newer_than:7d"
MAX_PER_SWEEP = 25


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
    # Make sure the labels we're about to assign actually exist in this account
    # (self-heals accounts that never completed the initial label provisioning).
    _ensure_labels_once(user_id)
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
