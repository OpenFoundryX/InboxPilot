"""Celery beat / scheduler definitions.

Maps a schedule name to a task + interval. Referenced by celery_app.conf.
"""

from celery.schedules import crontab

beat_schedule: dict = {
    # "nightly-reconciliation": {
    #     "task": "agents.tasks.run_reconciliation",
    #     "schedule": crontab(hour=2, minute=0),
    # },
}

__all__ = ["beat_schedule", "crontab"]
