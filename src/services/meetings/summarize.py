"""Turn a meeting transcript into a summary, decisions, and action items.

This is the step where the notetaker stops being a recording service. Same shape
as the other extractors in `services/`: OpenAI JSON mode, temperature 0, and a
tolerant parse — a malformed response costs us a recap, never a raised task.

Blocking; call from a worker.
"""

import json
from datetime import datetime
from functools import lru_cache
from typing import Any

from openai import OpenAI

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)

# gpt-4o-mini holds far more than this, but a long call is mostly filler and
# tokens cost money. Head and tail are kept because agendas are set at the start
# and commitments are made at the end — the middle is the expendable part.
MAX_TRANSCRIPT_CHARS = 60_000
_HEAD_SHARE = 0.6
_ELISION = "\n\n[… middle of the meeting omitted …]\n\n"

_SYS = """You summarize a meeting transcript for a participant who wants to skip rewatching it.

Return ONLY JSON:
{
  "summary": "<3-6 sentence plain-prose recap of what the meeting was about and where it landed>",
  "decisions": ["<a decision the group actually reached>"],
  "action_items": [
    {"what": "<the commitment, imperative and specific>",
     "owner": "<speaker name, or null if unclear>",
     "due_at": "<YYYY-MM-DDTHH:MM:SS, or null if no date was given>"}
  ]
}

Rules:
- Only include decisions and action items that were genuinely stated. An empty list is a valid, useful answer.
- Never invent an owner or a date. Use null.
- Resolve relative dates ("next Tuesday", "end of week") against the meeting time given to you.
- Speaker labels come from imperfect diarization; if attribution is ambiguous, use null rather than guessing."""


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _fit(transcript: str) -> str:
    """Truncate to the cap *including* the elision marker, not on top of it."""
    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        return transcript
    budget = MAX_TRANSCRIPT_CHARS - len(_ELISION)
    head = int(budget * _HEAD_SHARE)
    return f"{transcript[:head]}{_ELISION}{transcript[-(budget - head):]}"


def summarize(
    transcript: str,
    *,
    title: str | None = None,
    started_at: datetime | None = None,
    attendees: list[str] | None = None,
) -> dict[str, Any] | None:
    """Return `{summary, decisions, action_items}`, or None if extraction failed."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    if not transcript.strip():
        return None

    context = [f"Meeting: {title or '(untitled)'}"]
    if started_at:
        context.append(f"Started at: {started_at.isoformat()}")
    if attendees:
        context.append(f"Invited: {', '.join(attendees[:20])}")
    content = "\n".join(context) + f"\n\nTranscript:\n{_fit(transcript)}"

    try:
        resp = _client().chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYS},
                {"role": "user", "content": content},
            ],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        log.exception("meetings.summarize_failed", title=title)
        return None

    summary = str(data.get("summary") or "").strip()
    if not summary:
        return None
    return {
        "summary": summary,
        "decisions": _string_list(data.get("decisions")),
        "action_items": _action_items(data.get("action_items")),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _action_items(value: Any) -> list[dict]:
    """Keep only items with something to do; normalize the rest to null."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        what = str(item.get("what") or "").strip()
        if not what:
            continue
        owner = item.get("owner")
        due = item.get("due_at")
        out.append(
            {
                "what": what,
                "owner": str(owner).strip() if owner else None,
                "due_at": str(due).strip() if due else None,
            }
        )
    return out
