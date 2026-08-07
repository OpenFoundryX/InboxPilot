"""Why did the sweep skip everyone?

`drafts_sweep` returning `{"users": 0}` is ambiguous: `_due_users` drops a user
for three unrelated reasons and logs only one of them. This walks the same
gates in the same order and says which one each user fell out of.

    uv run python scripts/drafts_status.py

Read-only. Pass --make-due to reset `last_sweep_at` on every enabled user so
the next run has someone to work on.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select, update  # noqa: E402

from core.database import run_async, with_worker_session  # noqa: E402
from models.drafts import DraftSettings  # noqa: E402
from models.users import User  # noqa: E402
from services.billing.entitlements import FEATURE_DRAFT, check  # noqa: E402
from workers.jobs.drafts_sweep import SWEEP_INTERVAL_MINUTES, remaining_drafts  # noqa: E402


async def _rows(db):
    """Every settings row, including disabled ones — that is the common cause."""
    settings = list(await db.scalars(select(DraftSettings)))
    out = []
    now = datetime.now(UTC)
    delta = timedelta(minutes=SWEEP_INTERVAL_MINUTES)
    for row in settings:
        user = await db.scalar(select(User).where(User.id == row.user_id))
        last = row.last_sweep_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        due = last is None or (now - last) >= delta
        decision = await check(db, row.user_id, FEATURE_DRAFT, now=now)
        budget = await remaining_drafts(db, row.user_id, now)
        out.append(
            {
                "email": user.email if user else "(no user row)",
                "user_id": str(row.user_id),
                "enabled": row.is_enabled,
                "categories": list(row.category_keys or []),
                "last_sweep_at": last,
                "due": due,
                "entitled": decision.allowed,
                "reason": decision.reason,
                "budget": budget,
            }
        )
    return out


async def _make_due(db) -> int:
    old = datetime.now(UTC) - timedelta(days=1)
    result = await db.execute(
        update(DraftSettings)
        .where(DraftSettings.is_enabled.is_(True))
        .values(last_sweep_at=old)
    )
    return result.rowcount or 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--make-due",
        action="store_true",
        help="reset last_sweep_at on every enabled user, so the next sweep picks them up",
    )
    args = parser.parse_args()

    rows = run_async(with_worker_session(_rows))
    if not rows:
        print("No draft_settings rows at all — nobody has opened the drafts settings page.")
        print("The sweep only ever considers users with a row here.")
        return

    for r in rows:
        print(f"\n{r['email']}  ({r['user_id']})")
        verdict = "would be swept"
        if not r["enabled"]:
            verdict = "SKIPPED: drafting is off (is_enabled=False)"
        elif not r["categories"]:
            verdict = "SKIPPED: no categories selected (sweep_user returns 0 immediately)"
        elif not r["due"]:
            mins = SWEEP_INTERVAL_MINUTES
            verdict = f"SKIPPED: swept less than {mins}m ago ({r['last_sweep_at']})"
        elif not r["entitled"]:
            verdict = f"SKIPPED: not entitled ({r['reason']})"
        elif r["budget"] == 0:
            verdict = "SKIPPED: monthly draft quota exhausted"

        print(f"  enabled      : {r['enabled']}")
        print(f"  categories   : {r['categories'] or '(none)'}")
        print(f"  last_sweep_at: {r['last_sweep_at'] or '(never — counts as due)'}")
        print(f"  due          : {r['due']}")
        print(f"  entitled     : {r['entitled']}  ({r['reason']})")
        print(f"  quota left   : {'unlimited' if r['budget'] is None else r['budget']}")
        print(f"  -> {verdict}")

    if args.make_due:
        n = run_async(with_worker_session(_make_due))
        print(f"\nReset last_sweep_at on {n} enabled user(s). Run the sweep again.")


if __name__ == "__main__":
    main()
