"""Show what a catch-up sweep would draft, without drafting anything.

The sweep's expensive half is `generate_reply` — one LLM call per candidate —
and its visible half is a draft appearing in someone's mailbox. Neither is
reversible enough to be a good way to find out whether the Gmail query is
selecting what you expected, so this runs the selection half alone.

Two levels, cheapest first:

    # Which messages does the query find? Gmail only, no LLM, no drafts.
    uv run python scripts/sweep_dry_run.py you@example.com

    # Same, plus what `sweep_user` would do with them: which get drafted,
    # where the quota or the ceiling cuts the list off. Still no LLM.
    uv run python scripts/sweep_dry_run.py you@example.com --dry-run

Needs the database and Composio reachable — run it against a real environment
(`make up`, or with the app's `.env` loaded). It never writes anything.
"""

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from core.database import run_async, with_worker_session  # noqa: E402
from integrations.composio import gmail  # noqa: E402
from models.users import User  # noqa: E402
from services.categorization.store import get_or_create_categories  # noqa: E402
from services.drafts import sweep  # noqa: E402
from services.drafts.context import load_config  # noqa: E402
from workers.jobs.drafts_sweep import SECONDS_PER_DRAFT, remaining_drafts  # noqa: E402


async def _resolve_user(db, who: str) -> uuid.UUID:
    try:
        return uuid.UUID(who)
    except ValueError:
        pass
    found = await db.scalar(select(User).where(User.email == who))
    if found is None:
        raise SystemExit(f"No user with email {who!r}")
    return found.id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("user", help="user id or email address")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="also run sweep_user with drafting stubbed out",
    )
    args = parser.parse_args()

    uid: uuid.UUID = run_async(with_worker_session(lambda db: _resolve_user(db, args.user)))
    user_id = str(uid)

    config = run_async(with_worker_session(lambda db: load_config(db, uid)))
    print(f"user            : {user_id}")
    print(f"drafting enabled: {config.is_enabled}")
    print(f"categories      : {list(config.category_keys) or '(none — sweep returns 0)'}")

    if not config.is_enabled or not config.category_keys:
        print("\nNothing to do: drafting is off, or no categories are selected.")
        return

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    budget = run_async(with_worker_session(lambda db: remaining_drafts(db, uid, now)))
    limit = sweep.effective_limit(budget)
    print(f"quota remaining : {'unlimited' if budget is None else budget}")
    print(f"limit this pass : {limit}  (ceiling {sweep.SWEEP_SAFETY_CEILING})")

    categories = run_async(with_worker_session(lambda db: get_or_create_categories(db, uid)))
    targets = [
        (c.key, c.gmail_label)
        for c in categories
        if c.key in config.category_keys and c.is_enabled
    ]

    total = 0
    for category_key, gmail_label in targets:
        # Kept byte-identical to `sweep.sweep_user`'s query on purpose: the whole
        # point is to see what that pass would see, not something close to it.
        query = (
            f'label:"{gmail_label}" newer_than:{sweep.LOOKBACK_DAYS}d '
            f'-in:sent -in:draft -label:"{gmail.DRAFTED_LABEL}"'
        )
        print(f"\n--- {category_key}  ({gmail_label}) ---")
        print(f"query: {query}")
        try:
            emails = gmail.fetch_by_query(user_id, query, sweep.MAX_PER_CATEGORY, verbose=False)
        except Exception as exc:
            print(f"  FETCH FAILED: {exc}")
            continue

        print(f"  {len(emails)} candidate(s)")
        for e in emails:
            date = e.date.isoformat() if e.date else "(no date)"
            print(f"  - {date}  {(e.sender or '?')[:40]:40}  {(e.subject or '(no subject)')[:60]}")
        total += len(emails)

    print(f"\ncandidates found: {total}")
    print(f"would attempt   : {min(total, limit)}")
    print(f"est. pass length: ~{min(total, limit) * SECONDS_PER_DRAFT}s")
    if total > limit:
        print(f"  NOTE: {total - limit} left for the next pass (quota or ceiling).")

    if not args.dry_run:
        return

    # `sweep_user` calls `draft_reply` by module attribute, so replacing it here
    # intercepts every candidate without touching the LLM or the mailbox. The
    # count it returns is what the real pass would have created, minus whatever
    # the model would have declined — which is the one thing only a live run
    # can tell you.
    seen: list[str] = []

    def _fake_draft_reply(uid_arg, *, message_id, subject=None, **kwargs):
        seen.append(f"{message_id}  {(subject or '(no subject)')[:60]}")
        return f"dry-run-{len(seen)}"

    original = sweep.draft_reply
    sweep.draft_reply = _fake_draft_reply
    try:
        created = sweep.sweep_user(user_id, config, budget=budget)
    finally:
        sweep.draft_reply = original

    print(f"\n--- dry run: sweep_user would have drafted {created} ---")
    for line in seen:
        print(f"  {line}")
    print("\nNo LLM calls were made and no drafts were created.")


if __name__ == "__main__":
    main()
