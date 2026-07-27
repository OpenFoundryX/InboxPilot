"""Find the joinable meeting link in a calendar event, or in pasted text.

Google Calendar puts conferencing details in several places depending on how the
event was created: a native Meet link lands in `hangoutLink` or
`conferenceData.entryPoints`, while Zoom and Teams invitations usually arrive as
text in `location` or `description`. All of them have to work, so we check the
structured fields first and fall back to scanning prose.

Pure functions, no I/O — this is the part most likely to meet a shape we didn't
anticipate, so it stays trivially testable.
"""

import re

from models.meetings import PLATFORM_MEET, PLATFORM_TEAMS, PLATFORM_ZOOM

# Ordered: the first pattern that matches decides the platform. Anchored on the
# host so a URL merely *mentioning* a platform elsewhere can't match.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (PLATFORM_MEET, re.compile(r"https://meet\.google\.com/[a-z0-9\-_]+", re.I)),
    (PLATFORM_ZOOM, re.compile(r"https://[\w.\-]*zoom\.us/(?:j|w|s)/[^\s<>\"'\]);]+", re.I)),
    (
        PLATFORM_TEAMS,
        re.compile(r"https://teams\.(?:microsoft|live)\.com/l/meetup-join/[^\s<>\"'\]);]+", re.I),
    ),
)

# Trailing punctuation picked up from prose ("join at <url>." or "(<url>)").
_TRAILING = ".,;:)]}>\"'"


def find_meeting_link(text: str | None) -> tuple[str, str] | None:
    """Return `(url, platform)` for the first meeting link in `text`, else None."""
    if not text:
        return None
    for platform, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).rstrip(_TRAILING), platform
    return None


def link_from_event(event: dict) -> tuple[str, str] | None:
    """Return `(url, platform)` for a Google Calendar event, else None."""
    hangout = event.get("hangoutLink")
    if hangout:
        found = find_meeting_link(hangout)
        if found:
            return found

    for entry in (event.get("conferenceData") or {}).get("entryPoints") or []:
        if entry.get("entryPointType") not in (None, "video"):
            continue
        found = find_meeting_link(entry.get("uri") or entry.get("label"))
        if found:
            return found

    for field in ("location", "description", "summary"):
        found = find_meeting_link(event.get(field))
        if found:
            return found
    return None
