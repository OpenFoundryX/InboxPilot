"""Celery worker entrypoint.

Run with: celery -A worker.celery_app worker --loglevel=info

Importing the domain/agent task modules here registers their tasks with the
Celery app. Add new task modules to `TASK_MODULES` as domains grow.
"""

from workers.celery_app import celery_app

TASK_MODULES = [
    "workers.jobs.classify_new_email",
    "workers.jobs.reply_draft_job",
    "workers.jobs.sync_last_7_days",
    "agents.tasks",
]

celery_app.autodiscover_tasks(lambda: TASK_MODULES, force=True)

__all__ = ["celery_app"]
