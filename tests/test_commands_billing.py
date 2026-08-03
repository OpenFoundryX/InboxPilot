"""The chat/email "run this now" commands are a second entrance to the same
routine types `workers.jobs.routines_sweep` gates. `services.commands.handlers.execute`
must deny them the same way, or a user can get a Pro-only digest just by asking
for it by name instead of waiting for the scheduled sweep.
"""

import inspect

import pytest

from core.plans import INTERVAL_MONTHLY, PLAN_PRO, PLAN_STARTER
from models.billing import STATUS_ACTIVE, Subscription
from services.commands import handlers


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


def _forbid(monkeypatch, *names):
    """Fail the test loudly if any of these handlers functions gets called."""

    def _boom(*args, **kwargs):
        raise AssertionError("underlying routine function was called on a denied path")

    async def _aboom(*args, **kwargs):
        raise AssertionError("underlying routine function was called on a denied path")

    for name in names:
        target = getattr(handlers, name)
        monkeypatch.setattr(handlers, name, _aboom if _is_coro(target) else _boom)


def _is_coro(fn) -> bool:
    return inspect.iscoroutinefunction(fn)


async def test_locked_user_is_told_their_subscription_isnt_active(db, user, monkeypatch):
    """No subscription at all -> locked, distinct from an out-of-plan message."""
    _forbid(monkeypatch, "compose_briefing")
    monkeypatch.setattr(handlers, "send_to_inbox", lambda *a, **k: "msg-id")

    result = await handlers.execute(db, user.id, {"type": "send_briefing_now"})

    assert "subscription isn't active" in result
    assert "Pro" not in result


@pytest.mark.parametrize(
    "atype,fn_name",
    [
        ("catch_up_now", "compose_catchup"),
        ("summarize_invoices_now", "summarize_invoices"),
        ("scan_deadlines_now", "scan_deadlines"),
    ],
)
async def test_starter_cannot_run_pro_only_routines_on_demand(
    db, user, monkeypatch, atype, fn_name
):
    """Starter is entitled only to the briefing; the other three "_now" actions
    must be denied and must never reach the underlying compose/summarize/scan
    function — a returned message alone wouldn't prove the gate actually
    stopped the work.
    """
    await _subscribe(db, user, PLAN_STARTER)
    _forbid(monkeypatch, fn_name)
    monkeypatch.setattr(handlers, "send_to_inbox", lambda *a, **k: "msg-id")

    result = await handlers.execute(db, user.id, {"type": atype})

    assert "Pro" in result
    assert "subscription isn't active" not in result


async def test_starter_may_still_send_briefing_now(db, user, monkeypatch):
    """Starter's one entitled routine must still work through the "_now" path."""
    await _subscribe(db, user, PLAN_STARTER)

    calls: list[str] = []
    monkeypatch.setattr(
        handlers, "compose_briefing", lambda *a, **k: calls.append("briefing") or ("s", "b")
    )
    monkeypatch.setattr(handlers, "send_to_inbox", lambda *a, **k: "msg-id")

    result = await handlers.execute(db, user.id, {"type": "send_briefing_now"})

    assert calls == ["briefing"]
    assert result == "Sent your briefing"


@pytest.mark.parametrize(
    "atype,fn_name,expected",
    [
        ("send_briefing_now", "compose_briefing", "Sent your briefing"),
        ("catch_up_now", "compose_catchup", "Sent your catch-up"),
        ("summarize_invoices_now", "summarize_invoices", "Sent your invoice summary"),
    ],
)
async def test_pro_runs_every_sync_now_action(db, user, monkeypatch, atype, fn_name, expected):
    await _subscribe(db, user, PLAN_PRO)

    calls: list[str] = []
    monkeypatch.setattr(
        handlers, fn_name, lambda *a, **k: calls.append(fn_name) or ("subject", "body")
    )
    monkeypatch.setattr(handlers, "send_to_inbox", lambda *a, **k: "msg-id")

    result = await handlers.execute(db, user.id, {"type": atype})

    assert calls == [fn_name]
    assert result == expected


async def test_pro_runs_scan_deadlines_now(db, user, monkeypatch):
    await _subscribe(db, user, PLAN_PRO)

    calls: list[str] = []

    async def _fake_scan(db_, user_id, tz, lead_hours=24):
        calls.append("scan_deadlines")
        return 3

    monkeypatch.setattr(handlers, "scan_deadlines", _fake_scan)

    result = await handlers.execute(db, user.id, {"type": "scan_deadlines_now"})

    assert calls == ["scan_deadlines"]
    assert result == "Scanned for deadlines — set 3 reminder(s)"
