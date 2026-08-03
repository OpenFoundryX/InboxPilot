from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.config import settings as app_settings
from core.plans import INTERVAL_MONTHLY, PLAN_PRO, PLAN_STARTER
from models.billing import STATUS_ACTIVE, Subscription
from models.routines import ROUTINE_BRIEFING, ROUTINE_INVOICES
from services.billing.entitlements import FEATURE_ROUTINE, check

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


async def _subscribe(db, user, plan_id):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=plan_id,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
        )
    )
    await db.flush()


async def test_starter_runs_only_the_briefing(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    allowed = await check(db, user.id, FEATURE_ROUTINE, routine_type=ROUTINE_BRIEFING, now=NOW)
    denied = await check(db, user.id, FEATURE_ROUTINE, routine_type=ROUTINE_INVOICES, now=NOW)
    assert allowed.allowed is True
    assert denied.allowed is False


async def test_pro_runs_the_invoice_digest(db, user):
    await _subscribe(db, user, PLAN_PRO)
    decision = await check(db, user.id, FEATURE_ROUTINE, routine_type=ROUTINE_INVOICES, now=NOW)
    assert decision.allowed is True


async def test_locked_user_runs_nothing(db, user):
    decision = await check(db, user.id, FEATURE_ROUTINE, routine_type=ROUTINE_BRIEFING, now=NOW)
    assert decision.allowed is False


async def test_sweep_does_not_run_a_routine_the_plan_excludes(db, user, monkeypatch):
    """The gate must be wired into _sweep, not merely available to it."""
    from models.routines import Routine
    from workers.jobs import routines_sweep

    await _subscribe(db, user, PLAN_STARTER)

    # `_sweep` resolves the user's local time from `MailmanSettings.timezone`,
    # which `get_or_create_settings` defaults to `MAILMAN_DEFAULT_TZ` (not
    # UTC) for a user with no settings row yet. The routine's run_time must
    # be expressed in that same zone or the sweep's local-time check will
    # never match "now" and the test would pass without exercising the gate.
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(app_settings.MAILMAN_DEFAULT_TZ))
    db.add(
        Routine(
            user_id=user.id,
            type=ROUTINE_INVOICES,
            enabled=True,
            run_time=f"{now_local.hour:02d}:{now_local.minute:02d}",
        )
    )
    await db.flush()

    ran: list[str] = []

    async def _record(db_, routine, user_id, email, tz):
        ran.append(routine.type)

    monkeypatch.setattr(routines_sweep, "_run_routine", _record)
    await routines_sweep._sweep(db)

    assert ran == []


async def test_sweep_runs_a_routine_the_plan_allows(db, user, monkeypatch):
    from models.routines import Routine
    from workers.jobs import routines_sweep

    await _subscribe(db, user, PLAN_STARTER)

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(app_settings.MAILMAN_DEFAULT_TZ))
    db.add(
        Routine(
            user_id=user.id,
            type=ROUTINE_BRIEFING,
            enabled=True,
            run_time=f"{now_local.hour:02d}:{now_local.minute:02d}",
        )
    )
    await db.flush()

    ran: list[str] = []

    async def _record(db_, routine, user_id, email, tz):
        ran.append(routine.type)

    monkeypatch.setattr(routines_sweep, "_run_routine", _record)
    await routines_sweep._sweep(db)

    assert ran == [ROUTINE_BRIEFING]
