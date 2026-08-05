"""How much one catch-up pass is allowed to do.

The pass drafts everything undrafted in its window rather than trickling ten
per tick, so the only real bound is the user's remaining monthly quota. The
ceiling below it is a circuit breaker for unlimited plans, and it is coupled to
the lock TTL — a pass that outlives its lock lets the next tick spend the same
stale quota snapshot, which is the one error no later run can undo.
"""

from services.drafts import follow_up, sweep
from workers.jobs import drafts_sweep


def test_unlimited_quota_falls_back_to_the_safety_ceiling():
    assert sweep.effective_limit(None) == sweep.SWEEP_SAFETY_CEILING


def test_quota_binds_when_it_is_lower_than_the_ceiling():
    assert sweep.effective_limit(7) == 7


def test_exhausted_quota_drafts_nothing():
    assert sweep.effective_limit(0) == 0


def test_negative_quota_is_clamped_not_inverted():
    assert sweep.effective_limit(-5) == 0


def test_ceiling_is_well_above_the_old_per_sweep_trickle():
    """The point of the change: a normal backlog drains in one pass."""
    assert sweep.SWEEP_SAFETY_CEILING >= 100


def test_every_undrafted_message_in_the_window_is_fetched():
    """None makes `fetch_by_query` page to exhaustion rather than take the top N."""
    assert sweep.MAX_PER_CATEGORY is None


def test_follow_up_keeps_its_own_much_smaller_ceiling():
    """Nudges are not a backlog drain; they must not inherit the sweep's ceiling."""
    assert follow_up.MAX_PER_SWEEP < sweep.SWEEP_SAFETY_CEILING
    assert sweep.effective_limit(None, follow_up.MAX_PER_SWEEP) == follow_up.MAX_PER_SWEEP


def test_lock_ttl_outlasts_the_worst_case_pass():
    """The coupling that must not silently drift.

    Raising the ceiling without raising the TTL reintroduces the exact overshoot
    `DRAFTS_LOCK` exists to prevent.
    """
    worst_case = sweep.SWEEP_SAFETY_CEILING * drafts_sweep.SECONDS_PER_DRAFT
    assert drafts_sweep.DRAFTS_LOCK_TTL > worst_case


def test_lock_ttl_still_outlasts_the_beat_interval():
    """A TTL at or below the 300s beat is what caused the original bug."""
    assert drafts_sweep.DRAFTS_LOCK_TTL > 300
