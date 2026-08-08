"""Request and response contracts for scheduling.

Window validity (`start` before `end`) is enforced here rather than in the
routers. It was in a router, which meant it held for the one endpoint that
remembered to check and not for date overrides, and a nonsensical window
doesn't fail loudly downstream — it silently produces zero slots, which reads
to the host as "the product is broken" rather than "I typed 18:00-09:00".
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from core.config import settings as app_settings


def _clock(value: str) -> str:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (ValueError, AttributeError):
        raise ValueError("must be HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def _require_offset(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("must include a UTC offset")
    return value


class DayWindow(BaseModel):
    """A span of one day the host is available, in their own time zone."""

    start: str
    end: str

    _normalise = field_validator("start", "end")(_clock)

    @model_validator(mode="after")
    def ordered(self) -> "DayWindow":
        if self.start >= self.end:
            raise ValueError("a window must end after it starts")
        return self


class HoursWindow(DayWindow):
    weekday: Annotated[int, Field(ge=0, le=6)]


class QuestionDef(BaseModel):
    key: str = Field("", max_length=40)
    label: str = Field(min_length=1, max_length=200)
    type: Literal["text", "textarea", "select", "checkbox"] = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list, max_length=20)


# --------------------------------------------------------------------------
# Host-side: profile
# --------------------------------------------------------------------------


class SchedulingSettingsUpdate(BaseModel):
    slug: str | None = Field(None, min_length=3, max_length=80, pattern=r"^[a-z0-9-]+$")
    enabled: bool | None = None
    timezone: str | None = None
    weekly_hours: list[HoursWindow] | None = None
    include_link_in_drafts: bool | None = None
    confirmation_email: bool | None = None
    reschedule_reminders: bool | None = None


class SchedulingSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    enabled: bool
    timezone: str
    weekly_hours: list[HoursWindow]
    include_link_in_drafts: bool
    confirmation_email: bool
    reschedule_reminders: bool

    @computed_field
    @property
    def public_url(self) -> str:
        """Derived rather than assigned after construction.

        It was a plain field with an empty default that the router patched on
        the way out — which works until one of the (now several) places
        building this response forgets to, and ships a blank link to the UI.
        """
        return f"{app_settings.FRONTEND_BASE_URL}/schedule/{self.slug}"


# --------------------------------------------------------------------------
# Host-side: event types and overrides
# --------------------------------------------------------------------------


class EventTypeBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    enabled: bool = True
    position: int = 0
    duration_minutes: Annotated[int, Field(ge=5, le=480)] = 30
    slot_interval_minutes: Annotated[int, Field(ge=5, le=120)] = 15
    minimum_notice_minutes: Annotated[int, Field(ge=0, le=43_200)] = 120
    booking_horizon_days: Annotated[int, Field(ge=1, le=365)] = 60
    buffer_before_minutes: Annotated[int, Field(ge=0, le=240)] = 0
    buffer_after_minutes: Annotated[int, Field(ge=0, le=240)] = 0
    max_bookings_per_day: int | None = Field(None, ge=1, le=100)
    questions: list[QuestionDef] = Field(default_factory=list, max_length=15)


class EventTypeCreate(EventTypeBase):
    slug: str | None = Field(None, min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")


class EventTypeUpdate(BaseModel):
    slug: str | None = Field(None, min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    enabled: bool | None = None
    position: int | None = None
    duration_minutes: int | None = Field(None, ge=5, le=480)
    slot_interval_minutes: int | None = Field(None, ge=5, le=120)
    minimum_notice_minutes: int | None = Field(None, ge=0, le=43_200)
    booking_horizon_days: int | None = Field(None, ge=1, le=365)
    buffer_before_minutes: int | None = Field(None, ge=0, le=240)
    buffer_after_minutes: int | None = Field(None, ge=0, le=240)
    max_bookings_per_day: int | None = Field(None, ge=1, le=100)
    questions: list[QuestionDef] | None = None


class EventTypeRead(EventTypeBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    #: Filled in by the router, which is the only layer that knows whose event
    #: type this is. Defaulted so `model_validate` can run straight off the ORM
    #: row before the copy that sets it.
    profile_slug: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def stringify(cls, value: object) -> str:
        return str(value)

    @computed_field
    @property
    def public_url(self) -> str:
        return f"{app_settings.FRONTEND_BASE_URL}/schedule/{self.profile_slug}/{self.slug}"


class DateOverrideUpsert(BaseModel):
    day: date
    #: Empty means "unavailable all day" — the reason a host reaches for this.
    windows: list[DayWindow] = Field(default_factory=list, max_length=6)
    note: str | None = Field(None, max_length=200)


class DateOverrideRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    windows: list[DayWindow]
    note: str | None = None


# --------------------------------------------------------------------------
# Public: what a guest sees
# --------------------------------------------------------------------------


class PublicEventType(BaseModel):
    slug: str
    name: str
    description: str | None = None
    duration_minutes: int


class PublicProfile(BaseModel):
    slug: str
    host_name: str
    timezone: str
    event_types: list[PublicEventType]


class PublicEventDetail(BaseModel):
    slug: str
    host_name: str
    host_timezone: str
    event: PublicEventType
    questions: list[QuestionDef]
    #: Inclusive bounds a guest may book within, in the host's zone. Sent so
    #: the calendar can grey out unbookable dates instead of discovering them
    #: one rejected click at a time.
    first_bookable_day: date
    last_bookable_day: date


class AvailabilityDay(BaseModel):
    date: date
    slots: list[datetime]


class AvailabilityRange(BaseModel):
    timezone: str
    duration_minutes: int
    days: list[AvailabilityDay]


class CreateBooking(BaseModel):
    starts_at: datetime
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    attendee_emails: list[EmailStr] = Field(default_factory=list, max_length=10)
    notes: str | None = Field(None, max_length=2000)
    answers: dict[str, Any] = Field(default_factory=dict)

    _aware = field_validator("starts_at")(_require_offset)


class RescheduleBooking(BaseModel):
    starts_at: datetime

    _aware = field_validator("starts_at")(_require_offset)


class CancelBooking(BaseModel):
    reason: str | None = Field(None, max_length=500)


class BookingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    starts_at: datetime
    ends_at: datetime
    booker_name: str
    booker_email: str
    attendee_emails: list[str]
    title: str
    notes: str | None = None
    answers: dict[str, str] = Field(default_factory=dict)
    status: str
    meeting_url: str | None = None
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    cancel_reason: str | None = None
    rescheduled_at: datetime | None = None

    @field_validator("id", mode="before")
    @classmethod
    def stringify(cls, value: object) -> str:
        return str(value)


class ManagedBooking(BaseModel):
    """The guest's own view of their booking, fetched with a management token."""

    booking: BookingRead
    host_name: str
    host_timezone: str
    profile_slug: str
    event_slug: str | None = None
    #: False once the event type is gone or the meeting is in the past — the UI
    #: hides the reschedule control rather than offering an action that 409s.
    can_reschedule: bool
