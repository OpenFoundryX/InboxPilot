"""How a Composio response is unwrapped.

Composio is not consistent about this and the inconsistency is invisible until
something downstream is quietly empty:

    EVENTS_LIST     -> {"data": {"items": [...]}}
    CREATE_EVENT    -> {"data": {"response_data": {"id": ..., "hangoutLink": ...}}}

Reading `data` directly worked for reads and silently returned nothing for
writes, so every booking stored `calendar_event_id = NULL` — which made cancel
and reschedule skip the calendar entirely, with no error anywhere.
"""

from types import SimpleNamespace

import pytest

from integrations.composio import calendar


class FakeTools:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def execute(self, slug, args, user_id=None):
        self.calls.append((slug, args, user_id))
        return self.response


@pytest.fixture
def composio(monkeypatch):
    def install(response):
        tools = FakeTools(response)
        monkeypatch.setattr(
            calendar, "get_composio", lambda: SimpleNamespace(tools=tools)
        )
        return tools

    return install


def created(**event):
    """The shape CREATE_EVENT actually returns."""
    return {"successful": True, "error": None, "data": {"response_data": event}}


# --------------------------------------------------------------------------
# Writes: payload is nested
# --------------------------------------------------------------------------


def test_create_event_returns_the_google_event_not_the_wrapper(composio):
    composio(created(id="evt_123", hangoutLink="https://meet.google.com/abc"))
    event = calendar.create_event(
        "user-1",
        title="Meeting",
        starts_at=_dt(),
        ends_at=_dt(),
        attendee_emails=[],
    )
    assert event["id"] == "evt_123"
    assert event["hangoutLink"] == "https://meet.google.com/abc"


def test_update_event_unwraps_the_same_way(composio):
    composio(created(id="evt_123", start={"dateTime": "2027-03-02T19:30:00+05:30"}))
    event = calendar.update_event(
        "user-1", "evt_123", starts_at=_dt(), ends_at=_dt()
    )
    assert event["id"] == "evt_123"


def test_update_event_resends_the_attendees(composio):
    """UPDATE_EVENT replaces the event rather than patching it.

    Leaving `attendees` out of the payload does not mean "leave them alone" —
    Google clears the list, silently removing the guest from their own meeting.
    The event stays `confirmed` on the host's calendar and disappears from the
    guest's, which is indistinguishable from a cancellation to the one person
    it matters to.
    """
    tools = composio(created(id="evt_123"))
    calendar.update_event(
        "user-1",
        "evt_123",
        starts_at=_dt(),
        ends_at=_dt(),
        attendee_emails=["guest@example.com"],
    )
    _, args, _ = tools.calls[0]
    assert args["attendees"] == ["guest@example.com"]


def test_update_event_resends_the_title_too(composio):
    """Same replace semantics — an omitted summary is a cleared summary."""
    tools = composio(created(id="evt_123"))
    calendar.update_event(
        "user-1", "evt_123", starts_at=_dt(), ends_at=_dt(), title="Coffee"
    )
    assert tools.calls[0][1]["summary"] == "Coffee"


def test_a_response_without_the_wrapper_still_works(composio):
    """Falls back to `data`, so this survives Composio normalising the shape."""
    composio({"successful": True, "data": {"id": "evt_123"}})
    event = calendar.create_event(
        "user-1", title="M", starts_at=_dt(), ends_at=_dt(), attendee_emails=[]
    )
    assert event["id"] == "evt_123"


# --------------------------------------------------------------------------
# Reads: payload is not nested
# --------------------------------------------------------------------------


def test_list_events_reads_items_from_an_unnested_response(composio):
    composio({"successful": True, "data": {"items": [{"id": "a"}, {"id": "b"}]}})
    assert len(calendar.list_events("user-1", _dt(), _dt())) == 2


def test_list_events_would_also_cope_if_reads_became_nested(composio):
    composio({"successful": True, "data": {"response_data": {"items": [{"id": "a"}]}}})
    assert len(calendar.list_events("user-1", _dt(), _dt())) == 1


# --------------------------------------------------------------------------
# Failures still raise
# --------------------------------------------------------------------------


def test_an_unsuccessful_response_raises(composio):
    composio({"successful": False, "error": "quota exceeded", "data": {}})
    with pytest.raises(RuntimeError, match="quota exceeded"):
        calendar.create_event(
            "user-1", title="M", starts_at=_dt(), ends_at=_dt(), attendee_emails=[]
        )


def test_deleting_an_already_gone_event_is_success(composio):
    """A retry after a partial cancellation must not fail the whole operation."""
    composio({"successful": False, "error": "404 not found", "data": {}})
    calendar.delete_event("user-1", "evt_123")


def test_deleting_raises_on_a_real_failure(composio):
    composio({"successful": False, "error": "500 internal", "data": {}})
    with pytest.raises(RuntimeError):
        calendar.delete_event("user-1", "evt_123")


def _dt():
    from datetime import datetime, timezone

    return datetime(2027, 3, 2, 12, 0, tzinfo=timezone.utc)
