import pytest

from core.plans import (
    CURRENCY,
    INTERVAL_ANNUAL,
    INTERVAL_MONTHLY,
    PLAN_PRO,
    PLAN_STARTER,
    PLANS,
    get_plan,
    razorpay_plan_id_for,
)
from models.routines import ROUTINE_BRIEFING, ROUTINE_INVOICES


def test_both_tiers_exist():
    assert set(PLANS) == {PLAN_STARTER, PLAN_PRO}


def test_starter_limits_match_the_marketing_site():
    e = get_plan(PLAN_STARTER).entitlements
    assert e.bot_hours_per_month == 5
    assert e.drafts_per_month == 20
    assert e.custom_categories is False
    assert e.video_retention_days == 7
    assert e.transcript_retention_days == 90


def test_pro_limits_match_the_marketing_site():
    e = get_plan(PLAN_PRO).entitlements
    assert e.bot_hours_per_month == 15
    assert e.drafts_per_month is None
    assert e.custom_categories is True
    assert e.video_retention_days == 30
    assert e.transcript_retention_days == 365


def test_starter_gets_the_briefing_routine_only():
    e = get_plan(PLAN_STARTER).entitlements
    assert ROUTINE_BRIEFING in e.allowed_routines
    assert ROUTINE_INVOICES not in e.allowed_routines


def test_pro_gets_every_routine():
    from models.routines import ROUTINE_BRIEFING as _b  # noqa: F401

    starter = get_plan(PLAN_STARTER).entitlements.allowed_routines
    pro = get_plan(PLAN_PRO).entitlements.allowed_routines
    assert starter < pro


def test_prices_are_in_cents():
    assert get_plan(PLAN_STARTER).monthly_price_cents == 1900
    assert get_plan(PLAN_STARTER).annual_price_cents == 18000
    assert get_plan(PLAN_PRO).monthly_price_cents == 3900
    assert get_plan(PLAN_PRO).annual_price_cents == 34800


def test_unknown_plan_raises():
    with pytest.raises(KeyError):
        get_plan("team")


def test_razorpay_plan_lookup_covers_every_combination():
    for plan_id in (PLAN_STARTER, PLAN_PRO):
        for interval in (INTERVAL_MONTHLY, INTERVAL_ANNUAL):
            assert razorpay_plan_id_for(plan_id, interval) is not None


def test_razorpay_plan_lookup_rejects_unknown_interval():
    with pytest.raises(KeyError):
        razorpay_plan_id_for(PLAN_PRO, "weekly")


def test_razorpay_plan_lookup_resolves_each_combination_independently(monkeypatch):
    # Guard against a copy-paste bug where two of the four (plan, interval)
    # keys accidentally read the same settings field. Empty-string defaults
    # would let such a bug pass a plain "is not None" check, so patch in four
    # distinct sentinel values and confirm each combination reads its own.
    from core.config import settings

    monkeypatch.setattr(settings, "RAZORPAY_PLAN_STARTER_MONTHLY", "plan_starter_monthly")
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_STARTER_ANNUAL", "plan_starter_annual")
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_PRO_MONTHLY", "plan_pro_monthly")
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_PRO_ANNUAL", "plan_pro_annual")

    resolved = {
        (plan_id, interval): razorpay_plan_id_for(plan_id, interval)
        for plan_id in (PLAN_STARTER, PLAN_PRO)
        for interval in (INTERVAL_MONTHLY, INTERVAL_ANNUAL)
    }

    assert resolved[(PLAN_STARTER, INTERVAL_MONTHLY)] == "plan_starter_monthly"
    assert resolved[(PLAN_STARTER, INTERVAL_ANNUAL)] == "plan_starter_annual"
    assert resolved[(PLAN_PRO, INTERVAL_MONTHLY)] == "plan_pro_monthly"
    assert resolved[(PLAN_PRO, INTERVAL_ANNUAL)] == "plan_pro_annual"
    # All four resolved to distinct values — no two keys collided.
    assert len(set(resolved.values())) == 4


def test_currency_is_usd():
    assert CURRENCY == "USD"
