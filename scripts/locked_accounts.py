"""Who gets locked out the moment the paywall comes back on.

Billing was switched off on 2026-08-18. Migration c8e2f4a10b57 backfills a
14-day trial for accounts that have no `subscriptions` row, but it deliberately
does NOT refresh a trial that has already expired: in production that would hand
free time to customers who genuinely churned, and no migration can tell those
two apart. The accepted cost is that an account whose trial ran out during the
billing-off window reads LOCKED the instant the rules are restored.

That cost is only acceptable if it is *visible* before the deploy, not
discovered by the customer. This lists exactly who it lands on, so each name can
be comped or lapsed on purpose.

    docker compose run --rm api python scripts/locked_accounts.py

Run it against the environment you are about to deploy to — the dev database and
production hold different accounts, and the dev answer tells you nothing about
the production one. Read-only: it writes nothing, and every user is judged by
calling the real `resolve_access`, so this cannot drift from the rule it
diagnoses.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from core.database import run_async, with_worker_session  # noqa: E402
from models.billing import Subscription  # noqa: E402
from models.users import User  # noqa: E402
from services.billing.access import ACCESS_ENTITLED, resolve_access  # noqa: E402


async def _survey(db) -> tuple[int, list[dict]]:
    """Every user, judged by the real access rule. Returns (total, locked)."""
    users = list(await db.scalars(select(User).order_by(User.created_at)))
    subs = {s.user_id: s for s in await db.scalars(select(Subscription))}
    now = datetime.now(timezone.utc)

    locked = []
    for user in users:
        # A user with no row at all is the common case here: nothing created one
        # while billing was off. `resolve_access(None, ...)` is LOCKED, which is
        # precisely the outcome worth listing.
        sub = subs.get(user.id)
        if resolve_access(sub, now) == ACCESS_ENTITLED:
            continue
        locked.append(
            {
                "email": user.email,
                "status": sub.status if sub else "(no subscription row)",
                "trial_ends_at": sub.trial_ends_at if sub else None,
                "comped": sub.comped if sub else False,
                "expired_trial": bool(
                    sub is not None and sub.trial_ends_at is not None and sub.trial_ends_at <= now
                ),
            }
        )
    return len(users), locked


def main() -> None:
    total, locked = run_async(with_worker_session(_survey))

    print(f"{total} account(s) checked against services.billing.access.resolve_access")
    print()

    for row in locked:
        ends = row["trial_ends_at"].isoformat() if row["trial_ends_at"] else "(none)"
        note = "  <- trial already expired" if row["expired_trial"] else ""
        print(f"  {row['email']}")
        print(f"    status       : {row['status']}")
        print(f"    trial_ends_at: {ends}{note}")
        print(f"    comped       : {row['comped']}")

    if locked:
        print()

    print(f"{len(locked)} account(s) will be locked when the paywall is restored.")

    if locked:
        print()
        print("Each of these needs a decision BEFORE deploy: mark the subscription")
        print("`comped` for anyone who should keep access, or accept the lapse")
        print("deliberately. Doing nothing means they discover it themselves.")


if __name__ == "__main__":
    main()
