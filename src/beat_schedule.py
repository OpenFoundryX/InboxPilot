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
    # Catch-up drafting for mail the arrival job missed. Every 5 minutes rather
    # than every minute: the task itself only acts on users whose own 15-minute
    # window is up, and each pass costs Gmail queries plus LLM calls, so a
    # tighter beat would just be idle wake-ups.
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
}

__all__ = ["beat_schedule", "crontab"]
