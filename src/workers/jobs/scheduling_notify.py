"""Celery task: send a booking notification outside the request.

Every one of these emails is best-effort and none of them changes any state —
the booking is already committed and the calendar already updated by the time
one is sent. Keeping them in the request meant a guest rescheduling waited on
two sequential Google round trips instead of one: the calendar update they
needed, and then a Gmail send they gained nothing from.
"""

from core.logging import get_logger
from services.scheduling import notifications
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="scheduling.notify")
def notify(user_id: str, to: str, subject: str, body: str) -> dict:
    return {"sent": notifications.send(user_id, to, subject, body)}
