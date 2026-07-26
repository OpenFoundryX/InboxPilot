"""Pydantic schemas for the Mailman settings/VIP API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from models.mailman import MODE_CUSTOM_DAILY, MODE_INTERVAL, MODE_TIMES

DeliveryMode = str  # one of MODE_INTERVAL / MODE_TIMES / MODE_CUSTOM_DAILY
_MODES = {MODE_INTERVAL, MODE_TIMES, MODE_CUSTOM_DAILY}


class SettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    is_active: bool
    timezone: str
    delivery_mode: str
    interval_hours: int | None = None
    interval_minutes: int | None = None
    times_per_day: int | None = None
    custom_times: list[str] = []
    active_window_start: str
    active_window_end: str
    dnd_enabled: bool
    dnd_start: str | None = None
    dnd_end: str | None = None
    last_delivery_at: datetime | None = None


class SettingsUpdate(BaseModel):
    """All fields optional — only provided fields are updated (PATCH-like PUT)."""

    timezone: str | None = None
    delivery_mode: str | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=24)
    interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    times_per_day: int | None = Field(default=None, ge=1, le=24)
    custom_times: list[str] | None = None
    active_window_start: str | None = None
    active_window_end: str | None = None
    dnd_enabled: bool | None = None
    dnd_start: str | None = None
    dnd_end: str | None = None


class VipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    domains: list[str] = []
    addresses: list[str] = []
    keywords: list[str] = []


class VipUpdate(BaseModel):
    domains: list[str] | None = None
    addresses: list[str] | None = None
    keywords: list[str] | None = None


class MailmanStatus(BaseModel):
    is_active: bool
    held_count: int
