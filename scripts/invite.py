"""Manage the signup allowlist.

Signups are invite-only while the first ~100 customers are onboarded by hand.
This is the whole admin surface for that list.

    uv run python scripts/invite.py add someone@example.com --note "call 2026-08-24"
    uv run python scripts/invite.py list
    uv run python scripts/invite.py list --unclaimed
    uv run python scripts/invite.py revoke someone@example.com

`list` also prints the claimed/total count, so "how many of the 100 are gone" is
one command.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from core.database import run_async, with_worker_session  # noqa: E402
from models.invites import InvitedEmail  # noqa: E402
from models.users import User  # noqa: E402
from services.auth.invites import normalize_email  # noqa: E402


async def _add(db, email: str, note: str | None) -> str:
    normalized = normalize_email(email)
    existing = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == normalized))
    if existing is not None:
        state = "already claimed" if existing.claimed_at else "still open"
        return f"{normalized} was already invited ({state}) — nothing changed."

    db.add(InvitedEmail(email=normalized, note=note))
    await db.commit()
    return f"Invited {normalized}."


async def _list(db, only: str | None) -> list[str]:
    stmt = select(InvitedEmail).order_by(InvitedEmail.invited_at)
    if only == "claimed":
        stmt = stmt.where(InvitedEmail.claimed_at.is_not(None))
    elif only == "unclaimed":
        stmt = stmt.where(InvitedEmail.claimed_at.is_(None))

    rows = list(await db.scalars(stmt))
    if not rows:
        return ["No invites match."]

    out = []
    claimed = 0
    for row in rows:
        who = ""
        if row.claimed_by_user_id:
            user = await db.get(User, row.claimed_by_user_id)
            who = f" by {user.email}" if user else " by (deleted user)"
        if row.claimed_at:
            claimed += 1
            state = f"claimed {row.claimed_at:%Y-%m-%d}{who}"
        else:
            state = "open"
        note = f"  [{row.note}]" if row.note else ""
        out.append(f"  {row.email:<40} {state}{note}")

    # Only meaningful for the unfiltered listing; with a filter the denominator
    # is the filtered set, which would be a misleading "x of 100".
    if only is None:
        out.append("")
        out.append(f"{claimed} claimed / {len(rows)} invited")
    return out


async def _revoke(db, email: str) -> str:
    normalized = normalize_email(email)
    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == normalized))
    if row is None:
        return f"{normalized} is not on the list."
    if row.claimed_at is not None:
        # Deleting this row would not remove their access — existing users
        # always pass the gate — it would only destroy the record of how they
        # got in. Real revocation needs User.is_active to become load-bearing.
        return (
            f"{normalized} already claimed their invite. Deleting the row would not\n"
            f"revoke their access (existing users always pass the signup gate), only\n"
            f"lose the record. Refusing."
        )

    await db.delete(row)
    await db.commit()
    return f"Revoked {normalized}."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="invite an email address")
    p_add.add_argument("email")
    p_add.add_argument("--note", default=None, help="who they are / where they came from")

    p_list = sub.add_parser("list", help="show the allowlist")
    group = p_list.add_mutually_exclusive_group()
    group.add_argument("--claimed", action="store_true")
    group.add_argument("--unclaimed", action="store_true")

    p_revoke = sub.add_parser("revoke", help="remove an unclaimed invite")
    p_revoke.add_argument("email")

    args = parser.parse_args()

    if args.command == "add":
        print(run_async(with_worker_session(lambda db: _add(db, args.email, args.note))))
    elif args.command == "list":
        only = "claimed" if args.claimed else "unclaimed" if args.unclaimed else None
        for line in run_async(with_worker_session(lambda db: _list(db, only))):
            print(line)
    elif args.command == "revoke":
        print(run_async(with_worker_session(lambda db: _revoke(db, args.email))))


if __name__ == "__main__":
    main()
