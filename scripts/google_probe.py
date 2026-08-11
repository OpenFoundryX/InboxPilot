"""Does a real Google grant actually support what the app needs?

Three behaviours differ between Composio's abstraction and the raw Google APIs,
each of them a silent failure rather than an error, and each one changes the
migration design if the answer is no. This answers all three against a live
grant before any wrapper code depends on them.

    docker compose exec api python scripts/google_probe.py <user-email>

1. **Send-as alias.** `services.notify` sends every assistant email as
   `you+inboxos@gmail.com`. `messages.send` only accepts a From that is the
   authenticated user or a verified send-as alias.
2. **Label colours.** `INBOXPILOT_LABELS` carries free-form hex; `labels.create`
   accepts only a fixed palette and 400s otherwise. A failure here breaks
   `ensure_labels`, and with it classification for the whole account.
3. **Meet link timing.** Conference creation is asynchronous, so `events.insert`
   can return before `hangoutLink` exists — and the booking flow persists that
   field immediately.

Writes are real but self-contained: the probe emails only the account itself,
and deletes the label and event it creates. Pass --keep to leave them behind.
"""

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from core.database import run_async, with_worker_session  # noqa: E402
from integrations.google.client import (  # noqa: E402
    CALENDAR_BASE,
    GMAIL_BASE,
    GoogleAPIError,
    google_request,
)
from integrations.google.credentials import get_connection  # noqa: E402
from models.users import User  # noqa: E402

PASS = "  \033[32mPASS\033[0m"
FAIL = "  \033[31mFAIL\033[0m"
WARN = "  \033[33mWARN\033[0m"


def _resolve_user(email: str) -> tuple[str, str]:
    async def _read(db):
        user = await db.scalar(select(User).where(User.email == email))
        return (str(user.id), user.email) if user else None

    found = run_async(with_worker_session(_read))
    if not found:
        sys.exit(f"no user with email {email!r}")
    return found


def probe_identity(user_id: str) -> str:
    """Confirm the grant works at all, and report the mailbox it points at."""
    print("\n[0] Grant and profile")
    state = get_connection(user_id, fresh=True)
    if state is None:
        sys.exit("  user has no google_connections row — complete /integrations/google/connect")
    if state.revoked:
        sys.exit(f"  grant is revoked: {state.email}")

    print(f"  scopes: {' '.join(sorted(state.scopes))}")
    profile = google_request(user_id, "GET", f"{GMAIL_BASE}/profile")
    address = profile.get("emailAddress", "")
    print(f"{PASS} authenticated as {address} (historyId={profile.get('historyId')})")
    return address


def probe_send_alias(user_id: str, address: str, keep: bool) -> None:
    """Question 1: will Gmail accept a +alias in From?"""
    print("\n[1] Send-as +alias  (services/notify.py:35)")

    local, _, domain = address.partition("@")
    alias = f"{local}+inboxos@{domain}"

    listed = google_request(user_id, "GET", f"{GMAIL_BASE}/settings/sendAs")
    registered = {row.get("sendAsEmail") for row in listed.get("sendAs") or []}
    print(f"  registered send-as: {', '.join(sorted(a for a in registered if a))}")

    import base64
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = address
    msg["From"] = alias
    msg["Subject"] = "InboxPilot probe: send-as alias"
    msg.set_content("Probe message. Safe to delete.")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        sent = google_request(
            user_id,
            "POST",
            f"{GMAIL_BASE}/messages/send",
            json={"raw": raw},
            idempotent=False,
        )
    except GoogleAPIError as exc:
        print(f"{FAIL} Gmail rejected From={alias}: {exc}")
        print("       -> register the alias via settings.sendAs.create, or drop the")
        print("          alias and use a display name instead.")
        return

    message_id = sent.get("id")
    fetched = google_request(
        user_id,
        "GET",
        f"{GMAIL_BASE}/messages/{message_id}",
        params={"format": "metadata", "metadataHeaders": "From"},
    )
    headers = {h["name"].lower(): h["value"] for h in fetched.get("payload", {}).get("headers", [])}
    actual = headers.get("from", "")

    if alias in actual:
        print(f"{PASS} sent, and Gmail preserved From: {actual}")
    else:
        # The send succeeding is not the whole question — Gmail silently
        # rewriting From to the bare account would break the sender identity
        # just as thoroughly, only later and less visibly.
        print(f"{WARN} sent, but Gmail rewrote From to: {actual}")
        print("       -> the +alias identity does not survive; treat as a FAIL.")

    if not keep:
        google_request(
            user_id, "POST", f"{GMAIL_BASE}/messages/{message_id}/trash", expect_json=False
        )


def probe_label_colours(user_id: str, keep: bool) -> None:
    """Question 2: does Gmail accept the palette in INBOXPILOT_LABELS?"""
    print("\n[2] Label colours  (integrations/google/gmail.py)")

    from integrations.google.gmail import INBOXPILOT_LABELS

    existing = google_request(user_id, "GET", f"{GMAIL_BASE}/labels")
    taken = {(row.get("name") or "").casefold() for row in existing.get("labels") or []}

    rejected: list[tuple[str, str]] = []
    created: list[str] = []

    for name, colours in INBOXPILOT_LABELS.items():
        probe_name = f"{name}-probe"
        if probe_name.casefold() in taken:
            continue
        body = {
            "name": probe_name,
            "labelListVisibility": colours.get("label_list_visibility", "labelShow"),
            "messageListVisibility": "show",
            "color": {
                "backgroundColor": colours["background_color"],
                "textColor": colours["text_color"],
            },
        }
        try:
            made = google_request(user_id, "POST", f"{GMAIL_BASE}/labels", json=body)
            created.append(made.get("id"))
        except GoogleAPIError as exc:
            rejected.append((name, f"{colours['background_color']}/{colours['text_color']}"))
            print(f"{FAIL} {name}: {exc}")

    if not rejected:
        print(f"{PASS} all {len(INBOXPILOT_LABELS)} colour pairs accepted")
    else:
        print(f"       -> {len(rejected)} pair(s) outside Gmail's fixed palette:")
        for name, pair in rejected:
            print(f"          {name}: {pair}")

    if not keep:
        for label_id in created:
            google_request(
                user_id, "DELETE", f"{GMAIL_BASE}/labels/{label_id}", expect_json=False
            )


def probe_meet_link(user_id: str, address: str, keep: bool) -> None:
    """Question 3: is hangoutLink present on the insert response?"""
    print("\n[3] Meet link on create  (services/scheduling/booking.py:158)")

    start = datetime.now(timezone.utc) + timedelta(days=400)
    body = {
        "summary": "InboxPilot probe (safe to delete)",
        "description": "Probe event.",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
        "attendees": [{"email": address}],
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    event = google_request(
        user_id,
        "POST",
        f"{CALENDAR_BASE}/calendars/primary/events",
        # sendUpdates=none so the probe does not email anyone; the real wrapper
        # uses "all".
        params={"conferenceDataVersion": 1, "sendUpdates": "none"},
        json=body,
        idempotent=False,
    )

    event_id = event.get("id")
    link = event.get("hangoutLink")
    status = (
        (event.get("conferenceData") or {}).get("createRequest") or {}
    ).get("status", {}).get("statusCode")

    if link:
        print(f"{PASS} hangoutLink present immediately: {link}")
    else:
        print(f"{WARN} no hangoutLink on the insert response (status={status})")
        refetched = google_request(
            user_id, "GET", f"{CALENDAR_BASE}/calendars/primary/events/{event_id}"
        )
        if refetched.get("hangoutLink"):
            print(f"       -> appeared on re-fetch: {refetched['hangoutLink']}")
            print("          create_event must re-read before returning.")
        else:
            print("       -> still absent on re-fetch; check the Meet configuration.")

    if not keep:
        google_request(
            user_id,
            "DELETE",
            f"{CALENDAR_BASE}/calendars/primary/events/{event_id}",
            params={"sendUpdates": "none"},
            expect_json=False,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="app user whose grant to probe")
    parser.add_argument(
        "--keep", action="store_true", help="leave the probe label, message and event in place"
    )
    args = parser.parse_args()

    user_id, email = _resolve_user(args.email)
    print(f"probing grant for {email} ({user_id})")

    address = probe_identity(user_id)
    probe_send_alias(user_id, address, args.keep)
    probe_label_colours(user_id, args.keep)
    probe_meet_link(user_id, address, args.keep)
    print("\ndone.")


if __name__ == "__main__":
    main()
