"""Celery worker entrypoint.

Run with: celery -A worker.celery_app worker --loglevel=info

Importing the domain/agent task modules here registers their tasks with the
Celery app. Add new task modules to `TASK_MODULES` as domains grow.
"""

from workers.celery_app import celery_app

# Import every model module so all tables register on Base.metadata — needed so
# SQLAlchemy can resolve cross-model foreign keys (e.g. mailman -> users) inside
# worker tasks.
from models import auth as _auth_models  # noqa: F401,E402
from models import categorization as _categorization_models  # noqa: F401,E402
from models import drafts as _drafts_models  # noqa: F401,E402
from models import mailman as _mailman_models  # noqa: F401,E402
from models import meetings as _meetings_models  # noqa: F401,E402
from models import users as _users_models  # noqa: F401,E402

TASK_MODULES = [
    "workers.jobs.classify_new_email",
    "workers.jobs.handle_command_email",
    "workers.jobs.drafts_sweep",
    "workers.jobs.sync_last_7_days",
    "workers.jobs.reclassify",
    "workers.jobs.sync_category_inbox",
    "workers.jobs.mailman_tick",
    "workers.jobs.routines_sweep",
    "workers.jobs.reminders_sweep",
    "workers.jobs.meetings_sweep",
    "workers.jobs.process_meeting",
    "workers.jobs.transcribe_media",
    # `retention.sweep` has been in the beat schedule since retention shipped
    # but was never listed here, so no worker ever registered it and every
    # nightly dispatch was rejected as an unknown task. Media and transcripts
    # have therefore not actually been pruned.
    "workers.jobs.retention_sweep",
    # Ditto for `scheduling.reminders`: a beat entry without a line here is a
    # task the worker has never heard of, which fails silently at dispatch.
    "workers.jobs.scheduling_reminders",
    "workers.jobs.scheduling_notify",
    # The mail arrival path. `gmail.poll_all` and `gmail.renew_watches` are beat
    # entries and `gmail.poll_user` is dispatched by both the sweep and the push
    # webhook, so this module has to be registered or new mail simply stops being
    # processed — with nothing in the logs but a rejected task.
    "workers.jobs.gmail_poll",
    "agents.tasks",
]

celery_app.autodiscover_tasks(lambda: TASK_MODULES, force=True)

__all__ = ["celery_app"]
