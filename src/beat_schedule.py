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
    # Auto-label recently-arrived mail into the org labels.
    "classify-sweep": {
        "task": "classify.sweep",
        "schedule": 60.0,
    },
    # Execute commands the user emails to themselves.
    "commands-sweep": {
        "task": "commands.sweep",
        "schedule": 60.0,
    },
    # Run due user routines (briefings, digests, nudges).
    "routines-sweep": {
        "task": "routines.sweep",
        "schedule": 60.0,
    },
}

__all__ = ["beat_schedule", "crontab"]
