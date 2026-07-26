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
}

__all__ = ["beat_schedule", "crontab"]
