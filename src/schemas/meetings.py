"""Pydantic schemas for the meeting notetaker API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ActionItem(BaseModel):
    what: str
    owner: str | None = None
    due_at: str | None = None


class MeetingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    title: str | None = None
    meeting_url: str
    platform: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    attendees: list[str] = []
    status: str
    status_detail: str | None = None
    summary: str | None = None
    decisions: list[str] = []
    action_items: list[ActionItem] = []
    recap_sent_at: datetime | None = None


class MeetingDetail(MeetingRead):
    """Adds the transcript, which is too large to return in a list."""

    transcript: str | None = None


class JoinRequest(BaseModel):
    """Send a bot into a call now.

    `meeting_url` accepts a pasted invitation as well as a bare link — the
    server extracts the joinable URL.
    """

    meeting_url: str = Field(min_length=1)
    title: str | None = None


class SettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    auto_join: bool
    bot_name: str
    min_attendees: int
    skip_titles: list[str] = []
    lookahead_minutes: int
    email_recap: bool
    create_reminders: bool
    include_in_digest: bool


class SettingsUpdate(BaseModel):
    """All fields optional — only provided fields are updated."""

    enabled: bool | None = None
    auto_join: bool | None = None
    bot_name: str | None = Field(default=None, min_length=1, max_length=64)
    min_attendees: int | None = Field(default=None, ge=1, le=100)
    skip_titles: list[str] | None = None
    lookahead_minutes: int | None = Field(default=None, ge=1, le=1440)
    email_recap: bool | None = None
    create_reminders: bool | None = None
    include_in_digest: bool | None = None
