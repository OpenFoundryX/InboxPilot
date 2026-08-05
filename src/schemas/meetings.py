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
    # Null for uploads and browser recordings — there is no call to join, only
    # media that was captured some other way.
    meeting_url: str | None = None
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
    # Whether a video exists. Free to compute, unlike the link itself, so the
    # list can offer "watch" without a provider call per row.
    has_recording: bool = False


class MeetingDetail(MeetingRead):
    """Adds the transcript and a live video link, both too costly for a list."""

    transcript: str | None = None
    # A presigned provider link, resolved fresh when this is served. Treat it as
    # good for this page view only — it expires within hours, so it must not be
    # persisted or emailed by clients.
    recording_url: str | None = None
    recording_url_expires_at: datetime | None = None


class JoinRequest(BaseModel):
    """Send a bot into a call now.

    `meeting_url` accepts a pasted invitation as well as a bare link — the
    server extracts the joinable URL.
    """

    meeting_url: str = Field(min_length=1)
    title: str | None = None


class UploadRequest(BaseModel):
    """Announce a recording before sending it.

    Everything here is the client's claim about a file we have not seen. The
    size is checked against the plan limit now *and* signed into the upload
    permission, so a client that understates it is refused by the bucket rather
    than believed by us.
    """

    filename: str | None = Field(default=None, max_length=300)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    title: str | None = Field(default=None, max_length=300)
    # Attach the recording to a meeting already on the calendar — the "Link to
    # event" dropdown. Null means the upload stands on its own.
    calendar_event_id: str | None = Field(default=None, max_length=256)


class StartLiveRequest(BaseModel):
    """Begin recording in the browser.

    No size or type: neither is known when recording starts. The server picks
    both, since it is the one that has to sign an upload permission for a file
    that does not exist yet.
    """

    title: str | None = Field(default=None, max_length=300)


class UploadTarget(BaseModel):
    """Permission to PUT exactly one object, and the row it belongs to.

    The bytes go browser-to-bucket and never through this API — a gigabyte
    through FastAPI would pin a worker for minutes and pay egress twice. The
    client must send `headers` verbatim; they are part of what was signed.
    """

    meeting: MeetingRead
    upload_url: str
    headers: dict[str, str]
    expires_at: datetime


class EnableBotRequest(BaseModel):
    """Turn the notetaker on for an event already on the user's calendar.

    Covers both "never booked" and "booked then cancelled" — the calendar event
    is the stable identifier, since the Meeting row may not exist yet.
    """

    calendar_event_id: str = Field(min_length=1)


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
