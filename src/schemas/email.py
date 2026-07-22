from datetime import datetime

from pydantic import BaseModel


class EmailSummary(BaseModel):
    """A single Gmail message reduced to the fields we care about."""

    id: str | None = None
    thread_id: str | None = None
    sender: str | None = None
    to: str | None = None
    subject: str | None = None
    snippet: str | None = None  # short preview
    body: str | None = None  # full decoded plain-text body
    date: datetime | None = None
    labels: list[str] = []
