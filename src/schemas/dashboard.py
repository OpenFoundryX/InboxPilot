"""Response schemas for the dashboard home payload."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class DashboardUser(BaseModel):
    first_name: str


class DashboardSetup(BaseModel):
    # "syncing" until onboarding finishes, "ready" after.
    state: str
    initial_sync_at: datetime | None = None


class DashboardStats(BaseModel):
    emails_categorized: int
    drafts_created: int


class AgendaItem(BaseModel):
    calendar_event_id: str
    meeting_id: uuid.UUID | None = None
    title: str | None = None
    starts_at: datetime
    ends_at: datetime
    meeting_url: str | None = None
    # False when no bot is booked, or the booking was cancelled or failed.
    bot_on: bool
    # False once the call is underway or over — the UI disables rather than
    # offering a toggle the API would reject.
    bot_editable: bool


class DashboardMeetings(BaseModel):
    timezone: str
    today: list[AgendaItem] = []
    tomorrow: list[AgendaItem] = []


class DashboardSummary(BaseModel):
    user: DashboardUser
    setup: DashboardSetup
    stats: DashboardStats
    meetings: DashboardMeetings
