"""Regression coverage for scheduling settings API updates.

The original bug: `weekly_hours` arrives as a list of Pydantic models and has to
reach the JSONB column as plain dicts. Passing the models straight through
serialises to something SQLAlchemy cannot store, and the failure surfaces far
from the cause.
"""

import uuid
from types import SimpleNamespace

import pytest

from api.v1 import scheduling
from schemas.scheduling import SchedulingSettingsUpdate


class FakeDb:
    async def flush(self) -> None:
        return None


def fake_row() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        slug="alex",
        enabled=True,
        timezone="UTC",
        weekly_hours=[],
        include_link_in_drafts=True,
        confirmation_email=True,
        reschedule_reminders=True,
    )


@pytest.fixture
def patched(monkeypatch):
    """Stub the two collaborators `update_settings` reaches for."""
    row = fake_row()

    async def fake_settings(db, user):
        return row

    async def noop(_user_id):
        return None

    monkeypatch.setattr(scheduling.store, "get_or_create_settings", fake_settings)
    monkeypatch.setattr(scheduling.availability, "invalidate_busy", noop)
    return row


@pytest.mark.asyncio
async def test_update_settings_accepts_dumped_weekly_hours(patched) -> None:
    payload = SchedulingSettingsUpdate(
        weekly_hours=[{"weekday": 0, "start": "09:00", "end": "17:00"}]
    )

    result = await scheduling.update_settings(payload, SimpleNamespace(), FakeDb())

    assert patched.weekly_hours == [{"start": "09:00", "end": "17:00", "weekday": 0}]
    assert result.weekly_hours[0].start == "09:00"


@pytest.mark.asyncio
async def test_update_settings_rejects_an_unknown_timezone(patched) -> None:
    """A bad zone must be a 422, not a 500 from deep inside ZoneInfo."""
    from fastapi import HTTPException

    payload = SchedulingSettingsUpdate(timezone="Mars/Olympus")
    with pytest.raises(HTTPException) as raised:
        await scheduling.update_settings(payload, SimpleNamespace(), FakeDb())
    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_the_response_carries_the_public_url(patched) -> None:
    result = await scheduling.update_settings(
        SchedulingSettingsUpdate(), SimpleNamespace(), FakeDb()
    )
    assert result.public_url.endswith("/schedule/alex")
