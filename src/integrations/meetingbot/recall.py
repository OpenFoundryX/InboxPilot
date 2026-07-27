"""Recall.ai implementation of the meeting-bot boundary.

Recall runs the bot; we hand it a meeting URL and a `join_at`, then learn what
happened from status webhooks. When it reports `bot.done` the transcript is
behind a presigned URL reachable from the bot detail response.

Everything Recall-shaped stops here: status codes, payload nesting, and the
Standard Webhooks signature scheme are all translated into the neutral types in
`base.py`.
"""

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime
from functools import lru_cache
from typing import Any

import httpx

from core.config import settings
from core.logging import get_logger
from integrations.meetingbot.base import (
    BOT_DONE,
    BOT_ENDED,
    BOT_FAILED,
    BOT_JOINING,
    BOT_RECORDING,
    BotHandle,
    BotState,
    MeetingBotError,
    Transcript,
    TranscriptSegment,
    WebhookEvent,
)

log = get_logger(__name__)

# Recall's status codes -> our lifecycle. Anything unlisted is treated as
# `joining`: the bot exists and hasn't produced media yet, which is the only
# thing callers act on.
_STATUS_MAP = {
    "joining_call": BOT_JOINING,
    "in_waiting_room": BOT_JOINING,
    "in_call_not_recording": BOT_JOINING,
    "recording_permission_allowed": BOT_JOINING,
    "recording_permission_denied": BOT_FAILED,
    "in_call_recording": BOT_RECORDING,
    "call_ended": BOT_ENDED,
    "done": BOT_DONE,
    "fatal": BOT_FAILED,
}

# Signature headers. Recall moved to the vendor-neutral `webhook-*` names; the
# `svix-*` aliases are still sent for workspaces created before the switch.
#: Recall's way of saying there is no live call to command — the bot never
#: started, already left, or was terminated. Cancelling that is a no-op.
_ALREADY_OVER = re.compile(
    r"cannot_command_unstarted_bot|has not been started|bot_not_in_call|already.*(left|ended)",
    re.I,
)

_ID_HEADERS = ("webhook-id", "svix-id")
_TIMESTAMP_HEADERS = ("webhook-timestamp", "svix-timestamp")
_SIGNATURE_HEADERS = ("webhook-signature", "svix-signature")


@lru_cache(maxsize=1)
def _client() -> httpx.Client:
    if not settings.RECALL_API_KEY:
        raise MeetingBotError("RECALL_API_KEY is not configured")
    return httpx.Client(
        base_url=settings.RECALL_API_BASE.rstrip("/"),
        headers={
            "Authorization": f"Token {settings.RECALL_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=settings.RECALL_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def _request(method: str, path: str, **kwargs) -> dict:
    try:
        resp = _client().request(method, path, **kwargs)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise MeetingBotError(
            f"Recall {method} {path} failed: {exc.response.status_code} {exc.response.text[:300]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise MeetingBotError(f"Recall {method} {path} unreachable: {exc}") from exc
    if not resp.content:
        return {}
    return resp.json()


class RecallProvider:
    def schedule_bot(
        self,
        meeting_url: str,
        bot_name: str,
        *,
        join_at: datetime | None = None,
        meeting_id: str | None = None,
    ) -> BotHandle:
        payload: dict[str, Any] = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "recording_config": {
                # Streaming transcription, not `recallai_async`: async providers
                # are rejected here and must be kicked off as a separate job
                # after `recording.done`, which would mean two more webhook
                # events and a new status for no benefit to us. Streaming
                # transcribes during the call, so the text is ready the moment
                # the bot reports `done` — which is exactly when we ask for it.
                "transcript": {"provider": {"recallai_streaming": {}}},
                "participant_events": {},
            },
        }
        if join_at:
            payload["join_at"] = join_at.isoformat()
        if meeting_id:
            # Echoed back on every webhook, so a callback can find its row
            # without trusting a bot-id lookup.
            payload["metadata"] = {"meeting_id": meeting_id}

        data = _request("POST", "/api/v1/bot/", json=payload)
        bot_id = data.get("id")
        if not bot_id:
            raise MeetingBotError(f"Recall bot create returned no id: {str(data)[:200]}")
        return BotHandle(bot_id=str(bot_id))

    def cancel_bot(self, bot_id: str) -> None:
        """Delete a scheduled bot; if it is already in the call, make it leave.

        Which one applies depends on timing we don't control, so try the delete
        and fall back rather than reading state first and racing it.

        Idempotent: a bot that already finished, failed, or never started is
        already not going to join, which is what the caller asked for.
        """
        try:
            _request("DELETE", f"/api/v1/bot/{bot_id}/")
            return
        except MeetingBotError as exc:
            log.info("recall.delete_failed_trying_leave", bot_id=bot_id, error=str(exc))
        try:
            _request("POST", f"/api/v1/bot/{bot_id}/leave_call/")
        except MeetingBotError as exc:
            if _ALREADY_OVER.search(str(exc)):
                log.info("recall.cancel_noop", bot_id=bot_id)
                return
            raise

    def get_bot(self, bot_id: str) -> BotState:
        data = _request("GET", f"/api/v1/bot/{bot_id}/")
        code = ((data.get("status_changes") or [{}])[-1] or {}).get("code")
        recording = _first_recording(data)
        return BotState(
            bot_id=bot_id,
            status=_STATUS_MAP.get(code or "", BOT_JOINING),
            detail=code,
            recording_id=str(recording.get("id")) if recording.get("id") else None,
        )

    def fetch_transcript(self, bot_id: str) -> Transcript:
        data = _request("GET", f"/api/v1/bot/{bot_id}/")
        url = (
            _first_recording(data)
            .get("media_shortcuts", {})
            .get("transcript", {})
            .get("data", {})
            .get("download_url")
        )
        if not url:
            raise MeetingBotError(f"No transcript available for bot {bot_id}")

        # Presigned URL — must not carry our API token.
        try:
            resp = httpx.get(url, timeout=settings.RECALL_TIMEOUT_SECONDS)
            resp.raise_for_status()
            raw = resp.json()
        except httpx.HTTPError as exc:
            raise MeetingBotError(f"Transcript download failed for bot {bot_id}: {exc}") from exc

        return Transcript(segments=_parse_segments(raw))

    def parse_webhook(self, body: bytes, headers) -> WebhookEvent:
        _verify_signature(body, headers)

        payload = json.loads(body or b"{}")
        event = str(payload.get("event") or "")
        data = payload.get("data") or {}
        bot = data.get("bot") or {}
        inner = data.get("data") or {}

        code = inner.get("code") or event.removeprefix("bot.")
        detail = inner.get("sub_code") or code
        metadata = bot.get("metadata") or {}

        bot_id = bot.get("id")
        if not bot_id:
            raise MeetingBotError("Recall webhook has no bot id")

        return WebhookEvent(
            bot_id=str(bot_id),
            status=_STATUS_MAP.get(str(code), BOT_JOINING),
            detail=str(detail) if detail else None,
            meeting_id=metadata.get("meeting_id"),
        )


def _first_recording(bot: dict) -> dict:
    recordings = bot.get("recordings") or []
    return recordings[0] if recordings else {}


def _parse_segments(raw: Any) -> list[TranscriptSegment]:
    """Recall's transcript is one object per speaker turn, words split out.

    Rejoining words per turn is what makes the transcript readable and cheap to
    summarize; word-level timing is not something we use.
    """
    segments: list[TranscriptSegment] = []
    for turn in raw or []:
        if not isinstance(turn, dict):
            continue
        words = turn.get("words") or []
        text = " ".join(str(w.get("text", "")).strip() for w in words if w.get("text"))
        if not text.strip():
            continue
        start = None
        if words:
            start = ((words[0].get("start_timestamp") or {}) or {}).get("relative")
        participant = turn.get("participant") or turn
        segments.append(
            TranscriptSegment(
                speaker=participant.get("name"),
                text=text.strip(),
                start=float(start) if isinstance(start, (int, float)) else None,
            )
        )
    segments.sort(key=lambda s: s.start if s.start is not None else 0.0)
    return segments


def _header(headers, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = headers.get(name)
        if value:
            return value
    return None


def _verify_signature(body: bytes, headers) -> None:
    """Standard Webhooks HMAC: base64(HMAC-SHA256(`id.timestamp.body`)).

    The secret is `whsec_` + base64 key material; the key is the decoded bytes,
    not the string. The signature header carries a space-separated list of
    `v1,<sig>` entries so keys can be rotated — any match is good.
    """
    secret = settings.RECALL_WEBHOOK_SECRET
    if not secret:
        raise MeetingBotError("RECALL_WEBHOOK_SECRET is not configured")

    msg_id = _header(headers, _ID_HEADERS)
    timestamp = _header(headers, _TIMESTAMP_HEADERS)
    signature = _header(headers, _SIGNATURE_HEADERS)
    if not (msg_id and timestamp and signature):
        raise MeetingBotError("Recall webhook is missing signature headers")

    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = b".".join([msg_id.encode(), timestamp.encode(), body or b""])
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for part in signature.split():
        candidate = part.split(",", 1)[-1]
        if hmac.compare_digest(candidate, expected):
            return
    raise MeetingBotError("Recall webhook signature mismatch")
