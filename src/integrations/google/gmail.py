"""Gmail, called directly.

A drop-in replacement for the Composio module: same function names, same
signatures, same return types, so consumers change only their import line.

Every function takes `user_id` — the app user's UUID as a string — and resolves
the grant through `integrations.google.credentials`.

Blocking HTTP. Call from Celery tasks directly, or from async code via a
threadpool.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from core.config import settings
from core.logging import get_logger
from integrations.google.client import (
    GMAIL_BASE,
    GoogleAPIError,
    GoogleNotFound,
    google_request,
)
from integrations.google.credentials import ConnectionState, get_connection
from integrations.google.mime import (
    PREVIEW_BODY_CHARS,
    body_text,
    build_message,
    build_references,
    headers,
    message_date,
    reply_subject,
    snippet_of,
)
from models.categorization import BUILTIN_CATEGORIES
from models.google import GMAIL_REQUIRED_SCOPES
from schemas.email import EmailSummary

log = get_logger(__name__)

DRAFTED_LABEL = "inboxos-drafted"

# messages.list returns up to 500 ids in one call and costs 5 quota units
# regardless, so the page size is about response size, not quota.
FETCH_PAGE = 100
# Hard ceiling when fetching "all". Much lower than the Composio-era 2000:
# each message now costs its own 20-unit messages.get against a 6,000/minute
# per-user budget, so an unbounded fetch could eat a user's whole quota in one
# call. Callers that only need ids should use `list_message_ids`, which is
# ~400x cheaper and not subject to this cap.
FETCH_ALL_CAP = 300

INTERNAL_LABELS: dict[str, dict[str, str]] = {
    "inboxos-chat": {
        "background_color": "#2da2bb",
        "text_color": "#ffffff",
        "label_list_visibility": "labelShowIfUnread",
    },  # teal
    "inboxos-routines": {
        "background_color": "#ffad47",
        "text_color": "#000000",
        "label_list_visibility": "labelShowIfUnread",
    },  # orange
    "inboxos-later": {
        "background_color": "#f691b3",
        "text_color": "#000000",
        "label_list_visibility": "labelShowIfUnread",
    },  # pink
    "inboxos-rules": {
        "background_color": "#efa093",
        "text_color": "#000000",
        "label_list_visibility": "labelShowIfUnread",
    },  # salmon
    DRAFTED_LABEL: {
        "background_color": "#b9e4d0",
        "text_color": "#076239",
        "label_list_visibility": "labelShowIfUnread",
    },  # green
}

INBOXPILOT_LABELS: dict[str, dict[str, str]] = {
    **{
        builtin.gmail_label: {
            "background_color": builtin.color_bg,
            "text_color": builtin.color_text,
        }
        for builtin in BUILTIN_CATEGORIES
    },
    **INTERNAL_LABELS,
}

GMAIL_SYSTEM_LABEL_NAMES: frozenset[str] = frozenset(
    {
        "inbox",
        "spam",
        "trash",
        "unread",
        "starred",
        "important",
        "sent",
        "draft",
        "chat",
        "category_personal",
        "category_social",
        "category_promotions",
        "category_updates",
        "category_forums",
    }
)

RESERVED_LABEL_NAMES: frozenset[str] = (
    frozenset(name.casefold() for name in INBOXPILOT_LABELS) | GMAIL_SYSTEM_LABEL_NAMES
)


def _get(user_id: str, path: str, **kwargs: Any) -> Any:
    return google_request(
        user_id, "GET", f"{GMAIL_BASE}{path}", required_scopes=GMAIL_REQUIRED_SCOPES, **kwargs
    )


def _post(user_id: str, path: str, **kwargs: Any) -> Any:
    return google_request(
        user_id, "POST", f"{GMAIL_BASE}{path}", required_scopes=GMAIL_REQUIRED_SCOPES, **kwargs
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_active_connection(user_id: str) -> ConnectionState | None:
    """The user's live Gmail grant, or None."""
    state = get_connection(user_id)
    if state is None or state.revoked:
        return None
    return state if GMAIL_REQUIRED_SCOPES <= state.scopes else None


def is_connected(user_id: str) -> bool:
    return get_active_connection(user_id) is not None


def initiate_connection(user_id: str, callback_url: str | None = None) -> str:
    """Kept for signature compatibility; the grant is started from the API route.

    Connecting now needs a per-request CSRF token stored server-side, which is
    the route's job — there is nothing sensible to return from here.
    """
    raise NotImplementedError("start the Google grant via GET /v1/integrations/google/connect")


def get_profile(user_id: str) -> dict:
    """Mailbox profile: `emailAddress`, `messagesTotal`, `historyId`."""
    return _get(user_id, "/profile")


# ---------------------------------------------------------------------------
# Push notifications
# ---------------------------------------------------------------------------


def watch(user_id: str, topic: str) -> dict:
    """Ask Gmail to publish this mailbox's changes to a Pub/Sub topic.

    Returns `{"historyId": str, "expiration": str}` — expiration in epoch
    milliseconds, never more than 7 days out. Gmail does not renew watches, so
    the caller must persist that deadline and re-watch before it passes;
    letting it lapse stops push silently, with nothing raised anywhere.

    Calling this again for a mailbox that already has a watch is the documented
    way to renew, not an error — it replaces the existing one.

    No label filter is applied deliberately. Filtering to INBOX would miss mail
    that Mailman's hold filter strips INBOX from on delivery, which is exactly
    the mail most in need of classifying; the poller's own label check decides
    what is interesting, and it needs to see everything to do that.
    """
    return _post(user_id, "/watch", json={"topicName": topic})


def stop_watch(user_id: str) -> None:
    """Stop Gmail publishing changes for this mailbox. Safe if none exists."""
    try:
        _post(user_id, "/stop", expect_json=False)
    except GoogleNotFound:
        return


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def list_labels(user_id: str) -> list[dict]:
    return _get(user_id, "/labels").get("labels") or []


def _find_label_id(user_id: str, name: str) -> str | None:
    for label in list_labels(user_id):
        if (label.get("name") or "").casefold() == name.casefold():
            return label.get("id")
    return None


def _create_label(user_id: str, name: str, colours: dict[str, str] | None = None) -> dict:
    """Create a label, dropping the colour if Gmail rejects it.

    Gmail only accepts colours from a fixed palette and answers anything else
    with a 400. A label that exists without its colour is a cosmetic problem; a
    failed `ensure_labels` stops classification for the whole account, so the
    colour is the part that gives way.
    """
    body: dict[str, Any] = {
        "name": name,
        "labelListVisibility": (colours or {}).get("label_list_visibility", "labelShow"),
        "messageListVisibility": "show",
    }
    if colours and colours.get("background_color") and colours.get("text_color"):
        body["color"] = {
            "backgroundColor": colours["background_color"],
            "textColor": colours["text_color"],
        }

    try:
        return _post(user_id, "/labels", json=body)
    except GoogleAPIError as exc:
        if "color" not in str(exc).lower() or "color" not in body:
            raise
        log.warning("gmail.label_colour_rejected", user_id=user_id, label=name, error=str(exc))
        body.pop("color")
        return _post(user_id, "/labels", json=body)


def create_label(user_id: str, name: str) -> str:
    """Create an arbitrary Gmail label by name; return its id.

    NOT idempotent: Gmail answers a duplicate name with a 409, which surfaces
    here as a RuntimeError. Callers wanting create-or-lookup should check first
    (`_find_label_id`) or use `ensure_labels`.
    """
    try:
        return _create_label(user_id, name)["id"]
    except GoogleAPIError as exc:
        raise RuntimeError(f"Gmail label create failed for {name!r}: {exc}") from exc


def delete_label(user_id: str, name: str) -> bool:
    """Delete a Gmail label by name. True if it existed and was removed."""
    label_id = _find_label_id(user_id, name)
    if not label_id:
        return False
    try:
        google_request(
            user_id,
            "DELETE",
            f"{GMAIL_BASE}/labels/{label_id}",
            required_scopes=GMAIL_REQUIRED_SCOPES,
            expect_json=False,
        )
    except GoogleNotFound:
        return False
    except GoogleAPIError as exc:
        raise RuntimeError(f"Gmail label delete failed for {name!r}: {exc}") from exc
    return True


@dataclass(frozen=True)
class LabelSync:
    """Outcome of `ensure_labels`: what was created, and every label's id.

    `ids` maps casefolded label name -> Gmail label id, covering both the labels
    that already existed and the ones just created, so callers needing an id can
    read it here instead of paying for another labels.list round trip.
    """

    created: list[str]
    ids: dict[str, str]


def ensure_labels(user_id: str) -> LabelSync:
    """Ensure InboxPilot's organizational labels exist in the user's Gmail.

    Idempotent: lists what is there and creates only what is missing, matched
    case-insensitively since Gmail rejects duplicate names.
    """
    ids: dict[str, str] = {}
    for label in list_labels(user_id):
        name = (label.get("name") or "").casefold()
        if name and (label_id := label.get("id")):
            ids[name] = label_id

    created: list[str] = []
    for name, colours in INBOXPILOT_LABELS.items():
        if name.casefold() in ids:
            continue
        try:
            made = _create_label(user_id, name, colours)
        except GoogleAPIError as exc:
            raise RuntimeError(f"Gmail label create failed for {name!r}: {exc}") from exc
        if new_id := made.get("id"):
            ids[name.casefold()] = new_id
        created.append(name)

    return LabelSync(created=created, ids=ids)


# ---------------------------------------------------------------------------
# Modifying
# ---------------------------------------------------------------------------


def batch_modify(user_id: str, message_ids: list[str], add: list[str], remove: list[str]) -> None:
    """Apply one add/remove label set to many messages in a single call.

    Flat 50 quota units for up to 1000 ids, so batching is dramatically cheaper
    than per-message modifies. Answers 204 with no body.
    """
    if not message_ids:
        return
    for start in range(0, len(message_ids), 1000):
        _post(
            user_id,
            "/messages/batchModify",
            json={
                "ids": message_ids[start : start + 1000],
                "addLabelIds": add,
                "removeLabelIds": remove,
            },
            expect_json=False,
        )


def modify_thread(user_id: str, thread_id: str, add: list[str], remove: list[str]) -> None:
    """Apply one add/remove label set to every message in a thread."""
    _post(
        user_id,
        f"/threads/{thread_id}/modify",
        json={"addLabelIds": add, "removeLabelIds": remove},
    )


def trash_message(user_id: str, message_id: str) -> None:
    _post(user_id, f"/messages/{message_id}/trash", expect_json=False)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_email(
    user_id: str,
    to: str,
    subject: str,
    body: str,
    from_email: str | None = None,
    is_html: bool = False,
) -> str | None:
    """Send an email; return the sent message id.

    `from_email` must be the account itself or one of its verified send-as
    aliases — Gmail rejects anything else, unlike the Composio action which
    accepted the field unconditionally.
    """
    raw = build_message(to=to, subject=subject, body=body, from_email=from_email, is_html=is_html)
    # Never retried: a send that times out has usually already been delivered,
    # and a duplicate email cannot be taken back.
    sent = _post(user_id, "/messages/send", json={"raw": raw}, idempotent=False)
    return sent.get("id")


def create_draft(
    user_id: str, to: str, subject: str, body: str, thread_id: str | None = None
) -> str | None:
    """Create a draft reply (threaded when thread_id is given). Never sends."""
    in_reply_to = references = None
    if thread_id:
        in_reply_to, references, subject = _threading_headers(user_id, thread_id, subject)

    message: dict[str, Any] = {
        "raw": build_message(
            to=to,
            subject=subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )
    }
    if thread_id:
        message["threadId"] = thread_id

    draft = _post(user_id, "/drafts", json={"message": message}, idempotent=False)
    return draft.get("id")


def reply_in_thread(
    user_id: str, thread_id: str, to: str, body: str, is_html: bool = False
) -> str | None:
    """Reply within an existing thread, keeping the conversation threaded."""
    in_reply_to, references, subject = _threading_headers(user_id, thread_id, None)

    raw = build_message(
        to=to,
        subject=subject,
        body=body,
        is_html=is_html,
        in_reply_to=in_reply_to,
        references=references,
    )
    sent = _post(
        user_id,
        "/messages/send",
        json={"raw": raw, "threadId": thread_id},
        idempotent=False,
    )
    return sent.get("id")


def _threading_headers(
    user_id: str, thread_id: str, subject: str | None
) -> tuple[str | None, str | None, str]:
    """The In-Reply-To / References / Subject a reply into this thread needs.

    Gmail refuses a `threadId` whose subject does not match the thread's own, so
    this is not optional even when the caller supplied a subject — and
    `reply_in_thread` has no subject parameter at all.
    """
    try:
        thread = get_thread(
            user_id, thread_id, metadata_headers=["Message-ID", "References", "Subject"]
        )
    except GoogleAPIError:
        log.warning("gmail.thread_headers_unavailable", user_id=user_id, thread_id=thread_id)
        return None, None, reply_subject(subject)

    messages = thread.get("messages") or []
    if not messages:
        return None, None, reply_subject(subject)

    last = headers(messages[-1].get("payload") or {})
    message_id = last.get("message-id")
    return (
        message_id,
        build_references(last.get("references"), message_id),
        reply_subject(subject or last.get("subject")),
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get_message(user_id: str, message_id: str, *, fmt: str = "full") -> dict:
    return _get(user_id, f"/messages/{message_id}", params={"format": fmt})


def get_thread(user_id: str, thread_id: str, *, metadata_headers: list[str] | None = None) -> dict:
    """A thread and its messages.

    Passing `metadata_headers` switches to the metadata format, which skips the
    bodies — much smaller responses when only headers are wanted.
    """
    params: dict[str, Any] = {}
    if metadata_headers:
        params["format"] = "metadata"
        params["metadataHeaders"] = metadata_headers
    else:
        params["format"] = "full"
    return _get(user_id, f"/threads/{thread_id}", params=params)


def list_message_ids(
    user_id: str, query: str, max_results: int | None = None
) -> list[tuple[str, str]]:
    """(message_id, thread_id) for everything matching `query`.

    The cheap path. messages.list costs 5 quota units per page of up to 500,
    where actually reading those messages costs 20 units *each* — so a caller
    that only needs ids or thread ids should never go through `fetch_by_query`.
    Not subject to `FETCH_ALL_CAP`, because it is not expensive enough to need
    one.
    """
    out: list[tuple[str, str]] = []
    token: str | None = None
    limit = max_results if max_results is not None else 10_000

    while len(out) < limit:
        params: dict[str, Any] = {
            "q": query,
            "maxResults": min(500, limit - len(out)),
            "includeSpamTrash": False,
        }
        if token:
            params["pageToken"] = token

        page = _get(user_id, "/messages", params=params)
        for message in page.get("messages") or []:
            if message_id := message.get("id"):
                out.append((message_id, message.get("threadId") or ""))

        token = page.get("nextPageToken")
        if not token:
            break

    return out[:limit]


def fetch_by_query(
    user_id: str,
    query: str,
    max_results: int | None = 25,
    *,
    verbose: bool = False,
) -> list[EmailSummary]:
    """Fetch emails matching a Gmail search query.

    Lists ids, then reads each message. That second half is the expensive part —
    Gmail's list endpoint returns ids only — so `max_results=None` is capped at
    `FETCH_ALL_CAP` rather than running to exhaustion.

    `verbose` controls how much of the body is kept, not whether one is fetched:
    both response formats cost the same, and several callers read `.body` on
    non-verbose fetches. Non-verbose truncates to `PREVIEW_BODY_CHARS`.
    """
    limit = FETCH_ALL_CAP if max_results is None else max_results
    ids = list_message_ids(user_id, query, limit)

    if max_results is None and len(ids) >= FETCH_ALL_CAP:
        log.warning("gmail.fetch_all_capped", user_id=user_id, fetched=len(ids), cap=FETCH_ALL_CAP)

    return _fetch_messages(user_id, [message_id for message_id, _ in ids], verbose=verbose)


def _fetch_messages(user_id: str, ids: list[str], *, verbose: bool) -> list[EmailSummary]:
    """Read many messages in parallel, preserving order.

    A bounded pool rather than one request at a time: 25 sequential round trips
    to Gmail is several seconds of latency on paths that run inside a sweep.
    Bounded rather than unbounded because the per-user quota is finite and a
    burst of 300 concurrent reads would simply be rejected.
    """
    if not ids:
        return []

    workers = max(1, min(settings.GMAIL_FETCH_CONCURRENCY, len(ids)))

    def _one(message_id: str) -> EmailSummary | None:
        try:
            return _summarize(get_message(user_id, message_id), verbose=verbose)
        except GoogleNotFound:
            # Deleted between listing and reading. Normal on an active mailbox.
            return None
        except GoogleAPIError:
            log.warning("gmail.message_fetch_failed", user_id=user_id, message_id=message_id)
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, ids))

    return [summary for summary in results if summary is not None]


def fetch_recent_emails(
    user_id: str,
    days: int = 30,
    max_results: int | None = None,
) -> list[EmailSummary]:
    """Fetch the user's *received* mail from the last `days` days.

    `-from:me` because both callers are classification backfills, and a category
    label belongs on mail you were sent, not on mail you sent. It also keeps the
    backfill off self-addressed notes, which are the command surface.
    """
    return fetch_by_query(user_id, f"newer_than:{days}d -from:me", max_results)


def _summarize(message: dict, *, verbose: bool = True) -> EmailSummary:
    """Map a Gmail message resource to an EmailSummary."""
    payload = message.get("payload") or {}
    header_map = headers(payload)
    text, attachments = body_text(payload)

    if not verbose and len(text) > PREVIEW_BODY_CHARS:
        text = text[:PREVIEW_BODY_CHARS]

    return EmailSummary(
        id=message.get("id"),
        thread_id=message.get("threadId"),
        sender=header_map.get("from"),
        to=header_map.get("to"),
        subject=header_map.get("subject"),
        snippet=snippet_of(message, text),
        body=text or None,
        date=message_date(message, header_map),
        labels=message.get("labelIds") or [],
        attachments=attachments,
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def history_since(user_id: str, start_history_id: str, *, max_pages: int = 20) -> dict:
    """Mailbox changes since `start_history_id`.

    Returns `{"messages": [partial message dicts], "history_id": str | None}`.
    The message dicts carry `id`, `threadId` and `labelIds` only — enough to
    decide whether a message is worth reading, which matters because reading one
    costs 20 quota units and listening costs 2.

    Raises `GoogleNotFound` when the cursor has aged out; the caller must then
    reseed from `get_profile` rather than replay.
    """
    seen: set[str] = set()
    messages: list[dict] = []
    token: str | None = None
    latest: str | None = None

    for _ in range(max_pages):
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": 500,
        }
        if token:
            params["pageToken"] = token

        page = _get(user_id, "/history", params=params)
        latest = page.get("historyId") or latest

        for record in page.get("history") or []:
            for added in record.get("messagesAdded") or []:
                message = added.get("message") or {}
                message_id = message.get("id")
                # The same message legitimately appears in several history
                # records (added, then labelled), so dedupe before the caller
                # pays per message.
                if message_id and message_id not in seen:
                    seen.add(message_id)
                    messages.append(message)

        token = page.get("nextPageToken")
        if not token:
            break

    return {"messages": messages, "history_id": latest}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def create_filter(user_id: str, criteria: dict, action: dict) -> dict:
    return _post(user_id, "/settings/filters", json={"criteria": criteria, "action": action})


def list_filters(user_id: str) -> list[dict]:
    # Gmail names the array "filter", singular.
    return _get(user_id, "/settings/filters").get("filter") or []


def delete_filter(user_id: str, filter_id: str) -> None:
    """Delete a filter. An already-absent filter is success, not failure."""
    if not filter_id:
        return
    try:
        google_request(
            user_id,
            "DELETE",
            f"{GMAIL_BASE}/settings/filters/{filter_id}",
            required_scopes=GMAIL_REQUIRED_SCOPES,
            expect_json=False,
        )
    except GoogleNotFound:
        return
