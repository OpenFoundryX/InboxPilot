"""Pydantic schemas for daily notes."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class DailyNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    #: The calendar day, not an instant. Serializes as `YYYY-MM-DD`.
    note_date: date
    body: str
    updated_at: datetime | None = None


class DailyNoteWrite(BaseModel):
    """Save a day's notes.

    A blank body is meaningful rather than invalid: it means the user cleared
    the day, and the row is deleted. So there is no `min_length` here.
    """

    # Generous but bounded. A day's scratchpad that runs past this is a
    # document, and the cap is what stops one page from becoming unbounded
    # storage per user per day.
    body: str = Field(default="", max_length=100_000)
