"""Summary extraction: parse tolerantly, never invent, never raise."""

import json
from datetime import datetime, timezone

import pytest

from core.config import settings
from models.meetings import Meeting
from services.meetings import summarize as summarize_mod
from services.meetings.recap import compose_recap
from services.meetings.summarize import MAX_TRANSCRIPT_CHARS, _fit, summarize

TRANSCRIPT = "Sam: Are we shipping Friday?\nPriya: Yes, I'll have the migration ready."

GOOD_RESPONSE = {
    "summary": "The team agreed to ship on Friday once the migration lands.",
    "decisions": ["Ship on Friday"],
    "action_items": [
        {"what": "Finish the migration", "owner": "Priya", "due_at": "2026-07-30T17:00:00"},
        {"what": "Draft release notes", "owner": None, "due_at": None},
    ],
}


class FakeOpenAI:
    """Minimal stand-in for the OpenAI client's one call path."""

    def __init__(self, content: str | None):
        self._content = content
        self.chat = self  # chat.completions.create(...)
        self.completions = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        message = type("M", (), {"content": self._content})
        choice = type("C", (), {"message": message})
        return type("R", (), {"choices": [choice]})


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test", raising=False)


def patch_client(monkeypatch, content: str | None) -> FakeOpenAI:
    fake = FakeOpenAI(content)
    monkeypatch.setattr(summarize_mod, "_client", lambda: fake)
    return fake


def test_extracts_summary_decisions_and_actions(monkeypatch):
    patch_client(monkeypatch, json.dumps(GOOD_RESPONSE))
    result = summarize(TRANSCRIPT, title="Release sync")
    assert result["summary"].startswith("The team agreed")
    assert result["decisions"] == ["Ship on Friday"]
    assert len(result["action_items"]) == 2
    assert result["action_items"][0]["owner"] == "Priya"
    assert result["action_items"][1]["due_at"] is None


def test_returns_none_when_the_model_returns_no_summary(monkeypatch):
    patch_client(monkeypatch, json.dumps({"summary": "  ", "decisions": []}))
    assert summarize(TRANSCRIPT) is None


def test_malformed_json_does_not_raise(monkeypatch):
    patch_client(monkeypatch, "not json at all")
    assert summarize(TRANSCRIPT) is None


def test_empty_transcript_short_circuits(monkeypatch):
    patch_client(monkeypatch, json.dumps(GOOD_RESPONSE))
    assert summarize("   \n  ") is None


def test_drops_action_items_with_no_task(monkeypatch):
    patch_client(
        monkeypatch,
        json.dumps({"summary": "x", "action_items": [{"owner": "Sam"}, {"what": ""}]}),
    )
    assert summarize(TRANSCRIPT)["action_items"] == []


def test_ignores_wrong_shaped_lists(monkeypatch):
    patch_client(
        monkeypatch,
        json.dumps({"summary": "x", "decisions": "not a list", "action_items": "nope"}),
    )
    result = summarize(TRANSCRIPT)
    assert result["decisions"] == []
    assert result["action_items"] == []


def test_meeting_context_is_sent_to_the_model(monkeypatch):
    fake = patch_client(monkeypatch, json.dumps(GOOD_RESPONSE))
    summarize(
        TRANSCRIPT,
        title="Release sync",
        started_at=datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
        attendees=["sam@acme.com"],
    )
    sent = fake.kwargs["messages"][1]["content"]
    assert "Release sync" in sent
    assert "2026-07-26T10:00:00+00:00" in sent
    assert "sam@acme.com" in sent


def test_long_transcripts_keep_head_and_tail():
    long_text = ("a" * MAX_TRANSCRIPT_CHARS) + "TAIL_MARKER"
    fitted = _fit(long_text)
    # The marker counts against the budget — the cap is a real cap.
    assert len(fitted) <= MAX_TRANSCRIPT_CHARS
    assert fitted.startswith("aaa")
    assert fitted.endswith("TAIL_MARKER")
    assert "omitted" in fitted


def test_short_transcripts_are_untouched():
    assert _fit(TRANSCRIPT) == TRANSCRIPT


# --- recap rendering ---


def meeting(**overrides) -> Meeting:
    values = {
        "title": "Release sync",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "status": "processed",
        "attendees": ["sam@acme.com", "priya@acme.com"],
        "summary": GOOD_RESPONSE["summary"],
        "decisions": GOOD_RESPONSE["decisions"],
        "action_items": GOOD_RESPONSE["action_items"],
        "starts_at": datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return Meeting(**values)


def test_recap_includes_summary_decisions_and_actions():
    subject, body = compose_recap(meeting())
    assert subject == "📝 Recap: Release sync"
    assert GOOD_RESPONSE["summary"] in body
    assert "Ship on Friday" in body
    assert "Finish the migration (Priya — due 2026-07-30T17:00:00)" in body
    # An undated, unowned item still appears, just without the parenthetical.
    assert "• Draft release notes" in body


def test_recap_omits_empty_sections():
    _subject, body = compose_recap(meeting(decisions=[], action_items=[]))
    assert "Decisions" not in body
    assert "Action items" not in body


def test_recap_handles_a_missing_title():
    subject, _body = compose_recap(meeting(title=None))
    assert subject == "📝 Recap: your meeting"
