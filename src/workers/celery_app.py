from celery import Celery

from core.config import settings

celery_app = Celery(
    "myapp",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Retry the initial broker (RabbitMQ) connection instead of crashing if
    # the broker isn't up yet — needed since Celery 5.3.
    broker_connection_retry_on_startup=True,
)

# Beat schedule (imported lazily to avoid circular imports).
from beat_schedule import beat_schedule  # noqa: E402

celery_app.conf.beat_schedule = beat_schedule
