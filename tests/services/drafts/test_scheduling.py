"""Drafting is scheduled, not chained off classification.

Drafts used to be produced two ways: immediately when a message arrived
(`classify.new_email` chaining `drafts.reply`), and by a catch-up sweep for
whatever that missed. Two producers meant two code paths to keep honest and
drafts trickling into the mailbox one at a time as mail landed.

Now the sweep is the only producer, and it runs on a slower cadence so a batch
of drafts arrives together — the same argument the product already makes for
batching mail delivery rather than interrupting per message.
"""

import importlib

import pytest

from workers.jobs import classify_new_email as classify_module
from workers.jobs import drafts_sweep


def test_drafting_runs_every_two_hours():
    assert drafts_sweep.SWEEP_INTERVAL_MINUTES == 120


def test_classification_no_longer_knows_about_drafting():
    """The chain is gone, not merely disabled behind a flag."""
    assert not hasattr(classify_module, "reply_draft")
    source = importlib.import_module(classify_module.__name__).__doc__ or ""
    assert "reply_draft" not in dir(classify_module)
    assert "chain" not in source.lower() or "no longer" in source.lower()


def test_the_arrival_task_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("workers.jobs.reply_draft_job")


def test_worker_does_not_register_a_missing_task():
    """`worker.py` lists task modules explicitly; a stale entry breaks boot."""
    import worker

    assert "workers.jobs.reply_draft_job" not in worker.celery_app.conf.include


def test_sweep_still_covers_more_than_one_interval():
    """A pass must look back further than the gap between passes.

    Otherwise mail arriving just after a sweep, and older than the lookback by
    the next one, is never drafted at all — the gap the old arrival path used
    to cover.
    """
    from services.drafts import sweep

    lookback_minutes = sweep.LOOKBACK_DAYS * 24 * 60
    assert lookback_minutes > drafts_sweep.SWEEP_INTERVAL_MINUTES


def test_beat_still_ticks_faster_than_the_user_gate():
    """The beat decides nothing; it just gives the per-user gate a chance."""
    from beat_schedule import beat_schedule

    beat_seconds = beat_schedule["drafts-sweep"]["schedule"]
    assert beat_seconds < drafts_sweep.SWEEP_INTERVAL_MINUTES * 60
