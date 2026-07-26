"""Recall.ai boundary: webhook verification and transcript translation.

These are the two places a vendor change breaks us silently, and both are pure
functions of their input — no HTTP needed.
"""

import base64
import hashlib
import hmac
import json

import pytest

from core.config import settings
from integrations.meetingbot.base import (
    BOT_DONE,
    BOT_FAILED,
    BOT_RECORDING,
    MeetingBotError,
    Transcript,
)
from integrations.meetingbot.recall import RecallProvider, _parse_segments

SECRET = "whsec_" + base64.b64encode(b"super-secret-key-material").decode()
MSG_ID = "msg_2abc"
TIMESTAMP = "1785000000"


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "RECALL_WEBHOOK_SECRET", SECRET, raising=False)


def sign(body: bytes, *, secret: str = SECRET, msg_id: str = MSG_ID) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{msg_id}.{TIMESTAMP}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def payload(code: str = "in_call_recording", *, bot_id="bot_1", meeting_id="m-1") -> bytes:
    return json.dumps(
        {
            "event": f"bot.{code}",
            "data": {
                "data": {"code": code, "sub_code": None, "updated_at": "2026-07-26T10:00:00Z"},
                "bot": {"id": bot_id, "metadata": {"meeting_id": meeting_id}},
            },
        }
    ).encode()


def headers(body: bytes, *, prefix="webhook", **overrides) -> dict:
    h = {
        f"{prefix}-id": MSG_ID,
        f"{prefix}-timestamp": TIMESTAMP,
        f"{prefix}-signature": sign(body),
    }
    h.update(overrides)
    return h


def test_accepts_a_correctly_signed_webhook():
    body = payload()
    event = RecallProvider().parse_webhook(body, headers(body))
    assert event.bot_id == "bot_1"
    assert event.status == BOT_RECORDING
    assert event.meeting_id == "m-1"


def test_accepts_legacy_svix_header_names():
    body = payload()
    event = RecallProvider().parse_webhook(body, headers(body, prefix="svix"))
    assert event.status == BOT_RECORDING


def test_accepts_one_matching_signature_among_several():
    """Key rotation sends a space-separated list; any match is valid."""
    body = payload()
    h = headers(body)
    h["webhook-signature"] = f"v1,{base64.b64encode(b'wrong'*8).decode()} {sign(body)}"
    assert RecallProvider().parse_webhook(body, h).status == BOT_RECORDING


def test_rejects_tampered_body():
    body = payload()
    h = headers(body)
    with pytest.raises(MeetingBotError, match="signature mismatch"):
        RecallProvider().parse_webhook(payload(bot_id="bot_evil"), h)


def test_rejects_signature_from_another_secret():
    body = payload()
    other = "whsec_" + base64.b64encode(b"a-different-key").decode()
    h = headers(body, **{"webhook-signature": sign(body, secret=other)})
    with pytest.raises(MeetingBotError, match="signature mismatch"):
        RecallProvider().parse_webhook(body, h)


def test_rejects_replay_with_a_different_message_id():
    body = payload()
    h = headers(body)
    h["webhook-id"] = "msg_other"
    with pytest.raises(MeetingBotError, match="signature mismatch"):
        RecallProvider().parse_webhook(body, h)


def test_rejects_missing_headers():
    body = payload()
    with pytest.raises(MeetingBotError, match="missing signature headers"):
        RecallProvider().parse_webhook(body, {})


def test_rejects_when_no_secret_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "RECALL_WEBHOOK_SECRET", "", raising=False)
    body = payload()
    with pytest.raises(MeetingBotError, match="not configured"):
        RecallProvider().parse_webhook(body, headers(body))


@pytest.mark.parametrize(
    "code,expected",
    [
        ("done", BOT_DONE),
        ("fatal", BOT_FAILED),
        ("recording_permission_denied", BOT_FAILED),
        ("in_call_recording", BOT_RECORDING),
    ],
)
def test_maps_provider_codes_to_lifecycle(code, expected):
    body = payload(code)
    assert RecallProvider().parse_webhook(body, headers(body)).status == expected


def test_unknown_code_is_treated_as_joining():
    body = payload("some_future_code")
    assert RecallProvider().parse_webhook(body, headers(body)).status == "joining"


def test_webhook_without_bot_id_is_rejected():
    body = json.dumps({"event": "bot.done", "data": {"data": {"code": "done"}, "bot": {}}}).encode()
    with pytest.raises(MeetingBotError, match="no bot id"):
        RecallProvider().parse_webhook(body, headers(body))


# --- transcript translation ---

RAW = [
    {
        "participant": {"id": 2, "name": "Priya", "is_host": False},
        "words": [
            {"text": "Ship", "start_timestamp": {"relative": 12.0}},
            {"text": "it", "start_timestamp": {"relative": 12.4}},
        ],
    },
    {
        "participant": {"id": 1, "name": "Sam", "is_host": True},
        "words": [
            {"text": "Are", "start_timestamp": {"relative": 3.0}},
            {"text": "we", "start_timestamp": {"relative": 3.2}},
            {"text": "ready?", "start_timestamp": {"relative": 3.5}},
        ],
    },
]


def test_rejoins_words_into_speaker_turns():
    segments = _parse_segments(RAW)
    assert [(s.speaker, s.text) for s in segments] == [
        ("Sam", "Are we ready?"),
        ("Priya", "Ship it"),
    ]


def test_orders_turns_by_start_time():
    assert [s.start for s in _parse_segments(RAW)] == [3.0, 12.0]


def test_drops_turns_with_no_words():
    raw = [{"participant": {"name": "Sam"}, "words": []}, *RAW]
    assert len(_parse_segments(raw)) == 2


def test_survives_a_missing_participant_name():
    segments = _parse_segments([{"words": [{"text": "hello"}]}])
    assert segments[0].speaker is None
    assert segments[0].text == "hello"


def test_renders_as_labeled_lines():
    transcript = Transcript(segments=_parse_segments(RAW))
    assert transcript.render() == "Sam: Are we ready?\nPriya: Ship it"
    assert not transcript.is_empty


def test_empty_transcript_is_detected():
    assert Transcript(segments=[]).is_empty
    assert Transcript(segments=_parse_segments([])).is_empty


# --- cancel semantics ---


def test_cancel_is_a_noop_for_a_bot_that_never_started(monkeypatch):
    """Recall 400s on both paths for a terminated bot; that is still cancelled."""
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        if method == "DELETE":
            raise MeetingBotError("Recall DELETE failed: 400 not allowed")
        raise MeetingBotError(
            'Recall POST failed: 400 {"code":"cannot_command_unstarted_bot",'
            '"detail":"Cannot send a command to a bot which has not been started."}'
        )

    monkeypatch.setattr("integrations.meetingbot.recall._request", fake_request)
    RecallProvider().cancel_bot("bot_x")  # must not raise
    assert [m for m, _ in calls] == ["DELETE", "POST"]


def test_cancel_still_raises_on_a_real_provider_failure(monkeypatch):
    def fake_request(method, path, **kwargs):
        raise MeetingBotError("Recall unreachable: connection reset")

    monkeypatch.setattr("integrations.meetingbot.recall._request", fake_request)
    with pytest.raises(MeetingBotError, match="unreachable"):
        RecallProvider().cancel_bot("bot_x")


def test_cancel_stops_after_a_successful_delete(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "integrations.meetingbot.recall._request",
        lambda method, path, **kw: calls.append((method, path)) or {},
    )
    RecallProvider().cancel_bot("bot_x")
    assert calls == [("DELETE", "/api/v1/bot/bot_x/")]
