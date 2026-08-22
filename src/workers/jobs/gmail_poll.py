"""Finding new mail: walking Gmail's history and enqueueing the work.

`poll_user` is where every new message enters the app, whichever way it was
triggered — a Pub/Sub push from `api.v1.webhooks.gmail_push`, or the
reconciliation sweep below. Gmail's push notification says only "this mailbox
changed", never what changed, so there is exactly one lookup either way and no
second code path to keep in step.

Three tasks:

* `poll_all` — the reconciliation beat entry. Fans out; never talks to Gmail.
  This is the safety net, not the delivery path: push is best-effort, and a
  watch that lapses stops notifications with nothing raised anywhere. Running
  it slowly turns both of those from a silent outage into a bounded delay.
* `poll_user` — walks one mailbox's history.
* `renew_watches` — reinstalls `users.watch` before Gmail's 7-day cap expires.

Cheapness is the design constraint. `history.list` costs 2 quota units, while
reading a message costs 20, so the label filter below runs on the partial
message that history already returns and only survivors are ever fetched.
"""

from datetime import datetime, timedelta, timezone

from core.config import settings
from core.idempotency import claim_event_sync, is_ours_sync
from core.locks import single_run
from core.logging import get_logger
from integrations.google import gmail
from integrations.google.client import GoogleAPIError, GoogleNotFound
from integrations.google.credentials import (
    NotConnected,
    advance_history_id,
    get_connection,
    list_pollable_user_ids,
    list_watches_needing_renewal,
    set_history_id,
    set_watch_expiry,
)
from integrations.google.mime import (
    addressed_to,
    body_text,
    headers,
    recipient_addresses,
    snippet_of,
)
from integrations.google.oauth import GrantRevoked
from services.billing.gate import mail_gate_open
from services.mailman.gmail_ops import HOLD_LABEL_NAME, resolve_label_id
from workers.celery_app import celery_app
from workers.jobs.classify_new_email import classify_new_email
from workers.jobs.handle_command_email import handle_command_email

log = get_logger(__name__)

SENT_LABEL = "SENT"
INBOX_LABEL = "INBOX"
DRAFT_LABEL = "DRAFT"
TRASH_LABEL = "TRASH"

SNIPPET_CHARS = 200

# Spread each user's poll across the sweep. Without this every mailbox is read
# on the same second, which turns a steady trickle of requests into a periodic
# spike against a per-project rate limit.
POLL_WINDOW_SECONDS = 55

# Renew a watch this far ahead of Gmail's 7-day cap. Generous on purpose: the
# renewal sweep runs hourly, so this tolerates a long run of failures before
# push actually stops.
WATCH_RENEW_MARGIN = timedelta(days=2)

# A mailbox that has gone quiet for a long time can return a lot of history at
# once; this bounds one pass rather than letting it run unbounded.
MAX_MESSAGES_PER_POLL = 200

# When the cursor has aged out, this is the catch-up window. Deliberately
# narrow: a full mailbox resync belongs to onboarding, not to a recovery path
# that runs inside a 60-second beat.
RESET_CATCHUP_QUERY = "newer_than:1h"


@celery_app.task(name="gmail.poll_all")
def poll_all() -> dict:
    """Reconcile every connected mailbox against Gmail.

    Catches what push misses: a dropped notification, a watch that lapsed
    because renewal failed, or any mailbox that never had one installed.
    """
    if not settings.GMAIL_POLL_ENABLED:
        return {"skipped": "disabled"}

    with single_run("gmail-poll", ttl=POLL_WINDOW_SECONDS) as acquired:
        if not acquired:
            return {"skipped": "locked"}

        user_ids = list_pollable_user_ids()
        for index, user_id in enumerate(user_ids):
            poll_user.apply_async(
                (user_id,), countdown=index % POLL_WINDOW_SECONDS
            )
        return {"users": len(user_ids)}


@celery_app.task(name="gmail.renew_watches")
def renew_watches() -> dict:
    """Keep Gmail pushing, and install a watch on mailboxes that lack one.

    Gmail caps a watch at 7 days and renews nothing itself, so this is what
    stands between push working and push stopping. Renewing well before the
    deadline means a couple of consecutive failures are survivable rather than
    immediately fatal — and the reconciliation poll covers the gap regardless.
    """
    if not settings.GMAIL_PUSH_ENABLED:
        return {"skipped": "disabled"}
    if not settings.GOOGLE_PUBSUB_TOPIC:
        log.error("gmail.watch_topic_missing")
        return {"skipped": "no_topic"}

    cutoff = datetime.now(timezone.utc) + WATCH_RENEW_MARGIN
    user_ids = list_watches_needing_renewal(cutoff)

    renewed = failed = 0
    for user_id in user_ids:
        if install_watch(user_id):
            renewed += 1
        else:
            failed += 1

    if renewed or failed:
        log.info("gmail.watches_renewed", renewed=renewed, failed=failed)
    return {"renewed": renewed, "failed": failed}


def install_watch(user_id: str) -> bool:
    """Install or renew this mailbox's push watch. Never raises.

    Returns False on failure rather than propagating: one user's dead grant
    must not abort the renewal sweep for everyone else, and the consequence of
    a miss is degraded latency, not lost mail.
    """
    if not settings.GMAIL_PUSH_ENABLED or not settings.GOOGLE_PUBSUB_TOPIC:
        return False

    # A watch is a standing subscription to someone's mail. Neither install one
    # for a gated mailbox nor renew an existing one, so a lapsed account stops
    # generating push traffic instead of merely having it discarded downstream.
    if not mail_gate_open(user_id):
        return False

    try:
        result = gmail.watch(user_id, settings.GOOGLE_PUBSUB_TOPIC)
    except GrantRevoked:
        return False
    except (GoogleAPIError, NotConnected) as exc:
        log.warning("gmail.watch_failed", user_id=user_id, error=str(exc))
        return False

    expiration = result.get("expiration")
    if not expiration:
        log.warning("gmail.watch_no_expiration", user_id=user_id)
        return False

    try:
        expires_at = datetime.fromtimestamp(int(expiration) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        log.warning("gmail.watch_bad_expiration", user_id=user_id, value=expiration)
        return False

    set_watch_expiry(user_id, expires_at)
    log.info("gmail.watch_installed", user_id=user_id, expires_at=expires_at.isoformat())
    return True


@celery_app.task(name="gmail.poll_user")
def poll_user(user_id: str) -> dict:
    """Walk one mailbox's history and enqueue work for anything new."""
    # Before the lock, not inside it: a gated mailbox should not even contend
    # for one. Push notifications land here too, so this is the check that
    # stops an unpaid account being polled by its own incoming mail.
    if not mail_gate_open(user_id):
        return {"skipped": "gated"}

    with single_run(f"gmail-poll:{user_id}", ttl=120) as acquired:
        if not acquired:
            # A previous pass is still running. Skipping is right: it holds the
            # cursor we would otherwise walk from.
            return {"skipped": "locked"}
        return _poll(user_id)


def _poll(user_id: str) -> dict:
    state = get_connection(user_id, fresh=True)
    if state is None or state.revoked or not state.history_id:
        return {"skipped": "not_pollable"}

    cursor = state.history_id
    try:
        result = gmail.history_since(user_id, cursor)
    except GoogleNotFound:
        return _reset_cursor(user_id)
    except GrantRevoked:
        # Already recorded on the row by the credentials layer; the next
        # fan-out will not include this user.
        return {"skipped": "revoked"}
    except (GoogleAPIError, NotConnected) as exc:
        log.warning("gmail.poll_failed", user_id=user_id, error=str(exc))
        return {"error": str(exc)}

    messages = result["messages"][:MAX_MESSAGES_PER_POLL]
    queued = _dispatch(user_id, messages, account_email=state.email)

    if new_cursor := result.get("history_id"):
        _advance(user_id, cursor, new_cursor)

    if queued:
        log.info("gmail.poll", user_id=user_id, seen=len(messages), queued=queued)
    return {"seen": len(messages), "queued": queued}


def _advance(user_id: str, previous: str, new: str) -> None:
    """Move the cursor forward, never backwards.

    History ids are monotonically increasing integers rendered as strings, so
    comparing them as text would order "1000" before "999".
    """
    try:
        if int(new) <= int(previous):
            return
    except (TypeError, ValueError):
        return

    # Advanced only after every enqueue above has been issued: crashing between
    # the two replays the batch, which the event claim absorbs, whereas
    # advancing first would drop it silently.
    if not advance_history_id(user_id, previous=previous, new=new):
        log.info("gmail.cursor_moved_elsewhere", user_id=user_id)


def _reset_cursor(user_id: str) -> dict:
    """Recover from a cursor Gmail no longer recognises.

    Gmail expires history after roughly a week, so any mailbox that was
    unreachable for longer lands here. Reseed from the mailbox's current
    position and sweep a narrow recent window, rather than replaying everything.
    """
    try:
        profile = gmail.get_profile(user_id)
    except GoogleAPIError as exc:
        log.warning("gmail.history_reset_failed", user_id=user_id, error=str(exc))
        return {"error": "reset_failed"}

    history_id = profile.get("historyId")
    if not history_id:
        return {"error": "no_history_id"}

    set_history_id(user_id, str(history_id))
    log.warning("gmail.history_reset", user_id=user_id, history_id=history_id)

    try:
        recent = gmail.list_message_ids(user_id, RESET_CATCHUP_QUERY, MAX_MESSAGES_PER_POLL)
    except GoogleAPIError:
        return {"reset": True, "queued": 0}

    partials = [{"id": message_id, "threadId": thread_id} for message_id, thread_id in recent]
    return {
        "reset": True,
        "queued": _dispatch(
            user_id,
            partials,
            account_email=profile.get("emailAddress"),
            labels_known=False,
        ),
    }


def _dispatch(
    user_id: str,
    messages: list[dict],
    *,
    account_email: str | None,
    labels_known: bool = True,
) -> int:
    """Filter, guard, and enqueue. Returns how many were handed off."""
    hold_label_id = _hold_label_id(user_id)
    queued = 0

    for partial in messages:
        message_id = partial.get("id")
        if not message_id:
            continue

        if labels_known and not _is_interesting(partial.get("labelIds") or [], hold_label_id):
            continue

        is_command = SENT_LABEL in (partial.get("labelIds") or [])

        guarded = _guard(user_id, message_id, is_command)
        if guarded is None:
            continue
        if not guarded and is_command:
            continue

        try:
            message = gmail.get_message(user_id, message_id)
        except GoogleNotFound:
            continue
        except GoogleAPIError as exc:
            log.warning(
                "gmail.poll_message_failed", user_id=user_id, message_id=message_id, error=str(exc)
            )
            continue

        label_ids = message.get("labelIds") or []
        if not labels_known and not _is_interesting(label_ids, hold_label_id):
            continue

        if not _enqueue(user_id, message, label_ids, account_email):
            continue
        queued += 1

    return queued


def _is_interesting(label_ids: list[str], hold_label_id: str | None) -> bool:
    """Whether a message is one we process at all.

    Reproduces the old trigger query — `label:inbox OR label:"inboxos-later"` —
    plus SENT, which carries the self-emailed slash commands. SENT is a coarse
    filter on purpose: whether a sent message is *addressed to the user* can
    only be known once its headers are fetched, so `_enqueue` makes that call. Held mail has to
    be included: Mailman's filter strips INBOX on delivery, so watching INBOX
    alone would miss exactly the mail that most needs classifying before its
    batch is released.
    """
    if DRAFT_LABEL in label_ids or TRASH_LABEL in label_ids:
        return False
    if INBOX_LABEL in label_ids or SENT_LABEL in label_ids:
        return True
    return bool(hold_label_id and hold_label_id in label_ids)


def _hold_label_id(user_id: str) -> str | None:
    try:
        return resolve_label_id(user_id, HOLD_LABEL_NAME)
    except Exception:
        # Held mail is missed for this pass rather than the pass being lost.
        log.warning("gmail.hold_label_unresolved", user_id=user_id, exc_info=True)
        return None


def _guard(user_id: str, message_id: str, is_command: bool) -> bool | None:
    """Loop and duplicate guards. None means skip; False means Redis is down.

    The asymmetry when Redis is unavailable is deliberate and carried over from
    the webhook this replaced: classification is idempotent enough to risk, but
    a command can send mail, and running one twice — or running our own outgoing
    message as a command — is not recoverable.
    """
    try:
        if is_ours_sync(message_id):
            return None
        if not claim_event_sync(user_id, message_id):
            return None
    except Exception:
        log.exception("gmail.poll_guards_unavailable", user_id=user_id, message_id=message_id)
        return False
    return True


def _enqueue(
    user_id: str, message: dict, label_ids: list[str], account_email: str | None
) -> bool:
    """Hand one message to the task that owns it. Returns whether we did."""
    payload = message.get("payload") or {}
    header_map = headers(payload)
    message_id = str(message.get("id"))
    thread_id = message.get("threadId")

    if SENT_LABEL in label_ids:
        # The command surface is mail you send *to yourself*, which is the half
        # of the old `from:me to:me` sweep query that the SENT label does not
        # carry. Without this, every message you send anyone is parsed as a
        # command — and one that parses to no actions is answered as a question,
        # so an ordinary email to a colleague gets an assistant reply in its
        # thread. Mail sent to someone else is dropped here rather than
        # classified: the classifier is for mail you received.
        if not addressed_to(header_map, account_email):
            return False

        text, _ = body_text(payload)
        handle_command_email.delay(
            str(user_id),
            message_id,
            subject=header_map.get("subject"),
            body=text,
            thread_id=thread_id,
            label_ids=label_ids,
            recipients=recipient_addresses(header_map),
        )
        log.info("gmail.poll_command", user_id=user_id, message_id=message_id)
        return True

    classify_new_email.delay(
        str(user_id),
        message_id,
        sender=header_map.get("from"),
        subject=header_map.get("subject"),
        snippet=_snippet(message, payload),
        thread_id=thread_id,
        # No body and no recipients. Both were carried for auto-drafting, which
        # used to chain off this task; drafting is scheduled now and reads what
        # it needs from Gmail itself. Classification only ever wanted the
        # truncated preview above.
    )
    return True


def _snippet(message: dict, payload: dict) -> str | None:
    """Short preview for the classifier — never the whole body."""
    preview = snippet_of(message)
    if not preview:
        text, _ = body_text(payload)
        preview = text
    return preview.strip()[:SNIPPET_CHARS] or None
