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
    # How mail arrives. Gmail's Pub/Sub push (when enabled) delivers within
    # seconds; this sweep is what runs otherwise, and what covers push's gaps
    # when it is on — it is best-effort, and a lapsed watch stops it silently.
    #
    # 60s rather than the 15 minutes a pure safety net would need, because push
    # is not currently available here: an org policy blocks Gmail's service
    # account from publishing to the topic. This is the same cadence the
    # Composio trigger polled at, so mail latency matches what it always was.
    # `history.list` costs 2 quota units, so a mailbox checked every minute
    # spends ~120/hour against a 6,000/minute budget — the cost is the Celery
    # wake-ups, not the quota. Once push works, 900.0 is the right value again.
    "gmail-poll": {
        "task": "gmail.poll_all",
        "schedule": 60.0,
    },
    # Reinstall push watches before Gmail's 7-day cap expires. Hourly against a
    # 2-day renewal margin, so push survives a long run of failures — and picks
    # up any mailbox that never got a watch installed.
    "gmail-watch-renew": {
        "task": "gmail.renew_watches",
        "schedule": 3600.0,
    },
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
    # Email guests an hour before a booked meeting, with the link to move it.
    # Every 5 minutes: the reminder only has to land near its lead time, and
    # the job's own staleness window (30 min) is what stops a late tick from
    # sending a reminder after the meeting has begun.
    "scheduling-reminders": {
        "task": "scheduling.reminders",
        "schedule": 300.0,
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
