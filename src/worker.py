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
from models import mailman as _mailman_models  # noqa: F401,E402
from models import meetings as _meetings_models  # noqa: F401,E402
from models import users as _users_models  # noqa: F401,E402

TASK_MODULES = [
    "workers.jobs.classify_new_email",
    "workers.jobs.handle_command_email",
    "workers.jobs.reply_draft_job",
    "workers.jobs.sync_last_7_days",
    "workers.jobs.reclassify",
    "workers.jobs.mailman_tick",
    "workers.jobs.routines_sweep",
    "workers.jobs.reminders_sweep",
    "workers.jobs.meetings_sweep",
    "workers.jobs.process_meeting",
    "agents.tasks",
]

celery_app.autodiscover_tasks(lambda: TASK_MODULES, force=True)

__all__ = ["celery_app"]
