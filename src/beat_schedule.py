"""Celery beat / scheduler definitions.

Maps a schedule name to a task + interval. Referenced by celery_app.conf.
"""

from celery.schedules import crontab

beat_schedule: dict = {
    # Release held mail for any user whose delivery slot is due. Runs every
    # minute; the task itself decides who is due (per-user tz/schedule).
    "mailman-tick": {
        "task": "mailman.tick",
        "schedule": 60.0,
    },
    # Mail no longer appears here. Labeling and self-emailed commands are driven
    # by the Composio Gmail trigger (see api.v1.webhooks); the only remaining
    # non-webhook mail path is the onboarding backfill in jobs.sync_last_7_days.
    # Run due user routines (briefings, digests, nudges).
    "routines-sweep": {
        "task": "routines.sweep",
        "schedule": 60.0,
    },
    # Deliver due reminders.
    "reminders-sweep": {
        "task": "reminders.sweep",
        "schedule": 60.0,
    },
    # Book notetaker bots for meetings about to start, and recall bots whose
    # meeting was deleted. Every minute: the provider wants join_at in advance,
    # so a late sweep means a late bot.
    "meetings-sweep": {
        "task": "meetings.sweep",
        "schedule": 60.0,
    },
    # Scheduled drafting. The beat decides nothing — the task acts only on
    # users whose own `SWEEP_INTERVAL_MINUTES` window is up — so this only needs
    # to tick often enough that a due user is picked up promptly. Every 5
    # minutes gives at most that much lateness against a 2-hour cadence.
    "drafts-sweep": {
        "task": "drafts.sweep",
        "schedule": 300.0,
    },
    # Follow-up nudges for threads that went quiet. Hourly beat, daily per-user
    # gate — an hourly tick means a user who enables follow-ups does not wait up
    # to a day for the first one.
    "drafts-follow-up": {
        "task": "drafts.follow_up",
        "schedule": 3600.0,
    },
    # Enforce per-plan video/transcript retention windows. Daily is frequent
    # enough for windows measured in days, and keeps the job off the same
    # minute as the hot per-minute sweeps.
    "retention-sweep": {
        "task": "retention.sweep",
        "schedule": crontab(hour=4, minute=15),
    },
}

__all__ = ["beat_schedule", "crontab"]
