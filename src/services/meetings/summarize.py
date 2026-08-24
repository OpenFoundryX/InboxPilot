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

# A long call is mostly filler and tokens cost money, so there is still a cap —
# but sections are meant to cover what the meeting actually spent time on, and a
# dropped middle is a missing topic rather than a slightly thinner paragraph.
# 120k characters is roughly a three-hour meeting, so in practice nothing is
# elided; head and tail are still what survive when something must be, because
# agendas are set at the start and commitments made at the end.
MAX_TRANSCRIPT_CHARS = 120_000
_HEAD_SHARE = 0.6
_ELISION = "\n\n[… middle of the meeting omitted …]\n\n"

_SYS = """You summarize a meeting transcript for a participant who wants to skip rewatching it.

Return ONLY JSON:
{
  "title": "<a specific name for this meeting, 3-8 words, as a participant would label it>",
  "overview": "<2-3 sentences: what this meeting was about and where it landed>",
  "sections": [
    {"title": "<the topic, as the group would name it — 2-5 words>",
     "bullets": ["<a specific point made under that topic>"]}
  ],
  "decisions": ["<a decision the group actually reached>"],
  "action_items": [
    {"what": "<the commitment, imperative and specific>",
     "owner": "<speaker name, or null if unclear>",
     "due_at": "<YYYY-MM-DDTHH:MM:SS, or null if no date was given>"}
  ]
}

Sections are the substance — write them as notes a participant would have taken:
- One section per topic the meeting actually spent time on, in the order discussed. Most meetings have three to seven.
- Title them after the subject matter ("Upload Bills & OCR"), not the discourse ("Discussion", "Next topic").
- Bullets carry the specifics: numbers, names, limits, formats, constraints, disagreements. "Demo file-size limit set to 10MB; storage limit pending decision" is useful. "The team discussed limits" is not.
- Three to six bullets per section, each one line. Never pad a section to reach a count.

Rules:
- The title names the subject, not the format: "Claims assistant walkthrough" rather than "Team meeting" or "Weekly sync".
- Only include decisions and action items that were genuinely stated. An empty list is a valid, useful answer.
- Never invent an owner or a date. Use null.
- Resolve relative dates ("next Tuesday", "end of week") against the meeting time given to you.
- Speaker labels come from imperfect diarization; if attribution is ambiguous, use null rather than guessing."""

# Appended when the transcript carries no speaker labels at all — an upload or a
# browser recording, transcribed by a model that doesn't diarize. Without this
# the model reaches for the attendee list to fill in owners, and a recap that
# confidently assigns work to the wrong person is worse than one that assigns
# none. Naming a spoken name as the only admissible evidence leaves the genuine
# case ("Priya, can you send that?") working.
_UNLABELLED = """

This transcript has NO speaker labels — you cannot tell who is speaking.
Set every action item's "owner" to null unless someone is named out loud in the
text itself. Never infer an owner from the invited-attendee list."""


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _fit(transcript: str) -> str:
    """Truncate to the cap *including* the elision marker, not on top of it."""
    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        return transcript
    budget = MAX_TRANSCRIPT_CHARS - len(_ELISION)
    head = int(budget * _HEAD_SHARE)
    return f"{transcript[:head]}{_ELISION}{transcript[-(budget - head) :]}"


def summarize(
    transcript: str,
    *,
    title: str | None = None,
    started_at: datetime | None = None,
    attendees: list[str] | None = None,
    speakers_labelled: bool = True,
) -> dict[str, Any] | None:
    """Return `{summary, decisions, action_items}`, or None if extraction failed.

    `speakers_labelled` is False for transcripts with no diarization — uploads
    and browser recordings. It tightens the owner rule rather than changing the
    output shape.
    """
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
            model=settings.SUMMARY_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": _SYS if speakers_labelled else _SYS + _UNLABELLED,
                },
                {"role": "user", "content": content},
            ],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        log.exception("meetings.summarize_failed", title=title)
        return None

    summary = _compose(data)
    if not summary:
        return None
    return {
        "summary": summary,
        # Only used when nothing better exists — see `pipeline.finalize`. A
        # calendar organiser's own wording always wins over a guess from the
        # transcript, however good the guess.
        "title": str(data.get("title") or "").strip()[:300] or None,
        "decisions": _string_list(data.get("decisions")),
        "action_items": _action_items(data.get("action_items")),
    }


def _compose(data: Any) -> str:
    """Flatten the model's overview and sections into one Markdown document.

    Markdown rather than a new column, because `summary` is already the field
    every reader reaches for — the detail page renders it through the same
    Markdown component the chat uses, so headings and bullets arrive formatted
    with no schema change and nothing to migrate. The recap email flattens the
    same string back to plain text.

    Returns "" when there is nothing worth showing, which the caller treats as
    a failed extraction.
    """
    parts: list[str] = []

    overview = str(data.get("overview") or "").strip() if isinstance(data, dict) else ""
    if overview:
        parts.append(overview)

    for section in (data.get("sections") if isinstance(data, dict) else None) or []:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        bullets = [str(b).strip() for b in (section.get("bullets") or []) if str(b).strip()]
        # A title with nothing under it is a heading for an empty section, and
        # bullets with no title have nowhere to sit. Both are dropped rather
        # than rendered as a gap in the notes.
        if not title or not bullets:
            continue
        parts.append(f"## {title}\n" + "\n".join(f"- {b}" for b in bullets))

    return "\n\n".join(parts).strip()


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
