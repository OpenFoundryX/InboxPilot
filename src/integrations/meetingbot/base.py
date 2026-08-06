"""The meeting-bot vendor boundary.

Sending a participant into a Zoom/Meet/Teams call and getting clean per-speaker
audio back is undifferentiated work we buy. What we do with the transcript is
not. This module defines the whole surface of what we buy, so a second provider
(or a self-hosted bot) is a new file next to `recall.py` and a config change —
nothing above `integrations/` knows which vendor is in play.

Provider calls are blocking; call them from Celery workers, or via
`run_in_threadpool` from the API, matching the Composio integrations.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

# Provider-neutral bot lifecycle. Vendors report richer state; these are the
# distinctions that change what InboxPilot does next.
BOT_JOINING = "joining"  # connecting, waiting room, or joined but not recording
BOT_RECORDING = "recording"
BOT_ENDED = "ended"  # left the call, media not ready yet
BOT_DONE = "done"  # media and transcript available
BOT_FAILED = "failed"


class MeetingBotError(RuntimeError):
    """A provider call failed. Callers decide whether to retry or give up."""


@dataclass(frozen=True)
class BotHandle:
    """What we keep after asking for a bot."""

    bot_id: str


@dataclass(frozen=True)
class BotState:
    bot_id: str
    status: str
    detail: str | None = None
    recording_id: str | None = None


@dataclass(frozen=True)
class WebhookEvent:
    """A provider status callback, normalized.

    `meeting_id` is echoed back from the metadata we set at schedule time, so the
    webhook can find its row without a lookup on the vendor's bot id.
    """

    bot_id: str
    status: str
    detail: str | None = None
    meeting_id: str | None = None


@dataclass(frozen=True)
class RecordingMedia:
    """A pointer to the finished video, never the video itself.

    Vendors hand out short-lived signed links rather than permanent ones, so
    `expires_at` is part of the value: whoever caches `video_url` without it has
    cached a link that will quietly stop working. `recording_id` is the durable
    handle — the thing to keep, and to re-resolve a fresh link from.
    """

    video_url: str | None = None
    recording_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class MeetingDetails:
    """What the vendor learned about the call by being in it.

    All three fields are optional because all three genuinely go missing:
    Google Meet exposes no title at all, a bot that never got in has no start
    time, and a call the bot sat alone in has no participants. A caller fills
    the gaps it can and leaves the rest.
    """

    title: str | None = None
    #: Display names, in the order people first appeared. Not emails — the
    #: platforms mostly don't hand those over.
    participants: list[str] = field(default_factory=list)
    #: When recording actually began, which is the closest thing to when the
    #: meeting started. Not `join_at`, which is when we asked the bot to try.
    started_at: datetime | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    """One speaker's continuous run of speech."""

    speaker: str | None
    text: str
    start: float | None = None


@dataclass(frozen=True)
class Transcript:
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(s.text.strip() for s in self.segments)

    @property
    def is_diarized(self) -> bool:
        """Whether anything in here names who was speaking.

        False for transcripts from a model that doesn't separate speakers, which
        is every transcript of media a bot never attended. Readers use it to
        decide how much to trust attribution.
        """
        return any(s.speaker for s in self.segments)

    def render(self, max_chars: int | None = None) -> str:
        """Flatten to `Speaker: text` lines, oldest first.

        A transcript with no speakers anywhere renders as bare text instead.
        Prefixing every line with `Unknown:` would add nothing a reader can use
        and quietly spend a fifth of the summarizer's character budget saying
        the same word several hundred times.
        """
        diarized = self.is_diarized
        lines = [
            f"{s.speaker or 'Unknown'}: {s.text.strip()}" if diarized else s.text.strip()
            for s in self.segments
            if s.text.strip()
        ]
        text = "\n".join(lines)
        return text[:max_chars] if max_chars else text


@runtime_checkable
class MeetingBotProvider(Protocol):
    """Everything InboxPilot needs from a meeting-bot vendor."""

    def schedule_bot(
        self,
        meeting_url: str,
        bot_name: str,
        *,
        join_at: datetime | None = None,
        meeting_id: str | None = None,
    ) -> BotHandle:
        """Send a bot to `meeting_url`, at `join_at` if given, else immediately."""

    def cancel_bot(self, bot_id: str) -> None:
        """Recall a bot, whether it is scheduled or already in the call."""

    def get_bot(self, bot_id: str) -> BotState:
        """Current state of a bot."""

    def fetch_transcript(self, bot_id: str) -> Transcript:
        """The finished transcript. Only meaningful once the bot is `done`."""

    def fetch_recording(self, bot_id: str) -> RecordingMedia:
        """A link to the video recording. Only meaningful once the bot is `done`.

        A bot with no video yields an empty `RecordingMedia` rather than an
        error: audio-only calls and recordings still being assembled are normal,
        and the caller shows what it has. Raises only when the provider itself
        cannot be reached.
        """

    def fetch_details(self, bot_id: str) -> MeetingDetails:
        """What the bot learned about the call: title, participants, start.

        Only meaningful once the bot has been in the meeting. Costs more than
        `get_bot` — participant names may live behind a separate download — so
        this belongs in a worker, not on a webhook's critical path.

        Returns empty fields rather than raising when the vendor simply doesn't
        know; a missing title is normal, not a failure.
        """

    def parse_webhook(self, body: bytes, headers) -> WebhookEvent:
        """Verify and normalize a status callback. Raises on a bad signature."""
