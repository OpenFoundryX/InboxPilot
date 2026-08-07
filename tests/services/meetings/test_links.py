"""Meeting link extraction, one case per URL shape we have actually seen.

The Teams cases are the reason this file exists. The original pattern required
`/l/meetup-join/`, which was the only form Teams emitted when it was written.
Microsoft has since made `/meet/<id>` the default for personal accounts and
common in Business invites, and those silently did not match — so the meeting
was tracked with no URL and no platform, never got a bot, and a pasted link was
rejected as "no Teams link found".
"""

import pytest

from models.meetings import PLATFORM_MEET, PLATFORM_TEAMS, PLATFORM_ZOOM
from services.meetings.links import find_meeting_link, link_from_event

TEAMS_URLS = [
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=x",
    "https://teams.live.com/l/meetup-join/19:meeting_xyz@thread.v2/0",
    "https://teams.microsoft.com/meet/1234567890?p=AbCdEf",
    "https://teams.live.com/meet/9876543210",
    "https://teams.microsoft.com/meet/419876543210",
]


@pytest.mark.parametrize("url", TEAMS_URLS)
def test_every_teams_url_shape_is_recognised(url):
    found = find_meeting_link(url)
    assert found is not None, f"not matched: {url}"
    assert found[1] == PLATFORM_TEAMS


@pytest.mark.parametrize("url", TEAMS_URLS)
def test_teams_urls_survive_being_quoted_in_prose(url):
    """Invitations wrap links in punctuation; the whole URL must come back."""
    found = find_meeting_link(f"Join here: {url}.")
    assert found is not None
    assert found[0] == url


def test_meet_and_zoom_still_match():
    assert find_meeting_link("https://meet.google.com/abc-defg-hij")[1] == PLATFORM_MEET
    assert find_meeting_link("https://acme.zoom.us/j/98765?pwd=x")[1] == PLATFORM_ZOOM


def test_a_teams_mention_without_a_link_is_not_a_link():
    assert find_meeting_link("We'll use Microsoft Teams for this one.") is None
    assert find_meeting_link("https://teams.microsoft.com/") is None


def test_marketing_pages_are_not_meetings():
    """Anchored on the join path, so a product page cannot masquerade as one."""
    assert find_meeting_link("https://teams.microsoft.com/downloads") is None
    assert find_meeting_link("https://www.microsoft.com/en/microsoft-teams/group-chat") is None


def test_empty_and_missing_text():
    assert find_meeting_link(None) is None
    assert find_meeting_link("") is None


def test_teams_link_found_in_an_event_description():
    """The common path: a Teams invite accepted onto a Google Calendar."""
    event = {
        "summary": "Weekly sync",
        "description": "Join: https://teams.microsoft.com/meet/1234567890?p=AbCdEf",
    }
    found = link_from_event(event)
    assert found is not None
    assert found[1] == PLATFORM_TEAMS


def test_teams_link_found_in_an_event_location():
    event = {"location": "https://teams.live.com/meet/9876543210"}
    assert link_from_event(event)[1] == PLATFORM_TEAMS


def test_native_meet_link_still_wins_on_a_google_event():
    event = {
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "description": "Backup: https://teams.microsoft.com/meet/1234567890",
    }
    assert link_from_event(event)[1] == PLATFORM_MEET
