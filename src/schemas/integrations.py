from pydantic import BaseModel

from schemas.email import EmailSummary


class GmailStatus(BaseModel):
    connected: bool


class GmailConnect(BaseModel):
    redirect_url: str


class SyncQueued(BaseModel):
    task_id: str
    status: str = "queued"


class SyncResultData(BaseModel):
    """The payload returned by the sync_last_7_days Celery task."""

    user_id: str
    count: int
    emails: list[EmailSummary] = []


class SyncResult(BaseModel):
    task_id: str
    status: str  # Celery state: PENDING / STARTED / SUCCESS / FAILURE ...
    result: SyncResultData | None = None
    error: str | None = None
