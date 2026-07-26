"""Meeting-link extraction — the part most exposed to calendar-shape surprises."""

from models.meetings import PLATFORM_MEET, PLATFORM_TEAMS, PLATFORM_ZOOM
from services.meetings.links import find_meeting_link, link_from_event


def test_finds_google_meet_link_in_prose():
    text = "Join at https://meet.google.com/abc-defg-hij. See you then!"
    assert find_meeting_link(text) == ("https://meet.google.com/abc-defg-hij", PLATFORM_MEET)


def test_strips_trailing_punctuation_and_brackets():
    assert find_meeting_link("(https://meet.google.com/abc-defg-hij)")[0] == (
        "https://meet.google.com/abc-defg-hij"
    )


def test_finds_zoom_link_with_password_query():
    text = "Zoom: https://acme.zoom.us/j/98765432101?pwd=Abc123XyZ pass 4242"
    url, platform = find_meeting_link(text)
    assert platform == PLATFORM_ZOOM
    assert url == "https://acme.zoom.us/j/98765432101?pwd=Abc123XyZ"


def test_finds_teams_link():
    text = "Microsoft Teams meeting https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0"
    url, platform = find_meeting_link(text)
    assert platform == PLATFORM_TEAMS
    assert url.startswith("https://teams.microsoft.com/l/meetup-join/")


def test_ignores_unrelated_urls():
    assert find_meeting_link("Agenda doc: https://docs.google.com/document/d/xyz") is None
    assert find_meeting_link("Read https://zoom.us/pricing before the call") is None
    assert find_meeting_link("") is None
    assert find_meeting_link(None) is None


def test_event_prefers_hangout_link():
    event = {
        "hangoutLink": "https://meet.google.com/xyz-1234-abc",
        "description": "backup https://acme.zoom.us/j/111222333",
    }
    assert link_from_event(event) == ("https://meet.google.com/xyz-1234-abc", PLATFORM_MEET)


def test_event_reads_conference_entry_points():
    event = {
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1-555-0100"},
                {"entryPointType": "video", "uri": "https://meet.google.com/qqq-wwww-eee"},
            ]
        }
    }
    assert link_from_event(event)[0] == "https://meet.google.com/qqq-wwww-eee"


def test_event_falls_back_to_location_then_description():
    assert link_from_event({"location": "https://acme.zoom.us/j/555"})[1] == PLATFORM_ZOOM
    assert (
        link_from_event({"description": "Dial in: https://acme.zoom.us/j/555"})[1]
        == PLATFORM_ZOOM
    )


def test_event_without_any_link():
    assert link_from_event({"summary": "Lunch", "location": "Kitchen"}) is None
