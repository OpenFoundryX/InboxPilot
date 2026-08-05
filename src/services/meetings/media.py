"""Media we hold ourselves: reserving a key, and confirming the bytes arrived.

Uploads and browser recordings both end as an object in our bucket awaiting
transcription. They differ only in how the bytes were produced, so everything
from the key onward is shared and lives here.

The API calls these; the pure parts (key derivation, type and size validation)
have no I/O, which is what makes the interesting decisions inspectable without
a bucket.
"""

import re
import uuid
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.logging import get_logger
from integrations.storage import get_storage
from models.meetings import STATUS_RECORDED, Meeting

log = get_logger(__name__)

#: What a meeting recording can be. Anything ffmpeg can open would be a wider
#: net than we want: the point is to reject a PDF or a zip before it costs a
#: gigabyte of transfer, not to be maximally permissive.
ALLOWED_PREFIXES = ("audio/", "video/")

#: What the browser recorder produces. MediaRecorder emits WebM/Opus in every
#: browser that supports it, and Safari emits MP4 — the container is decided by
#: the browser, not by us, so the client sends its own and we validate the
#: family rather than dictating one.
LIVE_CONTENT_TYPE = "audio/webm"

#: Extensions worth preserving on the key. ffprobe sniffs the real container, so
#: this is only for humans reading the bucket; an unrecognized type gets none
#: rather than a guess, because a wrong extension is worse than no extension.
_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class MediaRejected(ValueError):
    """The file cannot be accepted. The message is shown to the user."""


def validate(content_type: str, size_bytes: int) -> str:
    """Check a declared upload, returning the normalized content type.

    Both checks happen before a single byte moves. The size is re-enforced in
    the presigned signature, so a client that declares one size and sends
    another is refused by the bucket — this check is about giving an honest
    client an honest error rather than about trusting one.
    """
    normalized = (content_type or "").split(";")[0].strip().lower()
    if not normalized.startswith(ALLOWED_PREFIXES):
        raise MediaRejected(
            f"{normalized or 'That file type'} isn't audio or video"
        )
    if size_bytes <= 0:
        raise MediaRejected("That file is empty")
    if size_bytes > settings.MEDIA_UPLOAD_MAX_BYTES:
        limit_gb = settings.MEDIA_UPLOAD_MAX_BYTES / (1024**3)
        raise MediaRejected(f"That file is larger than the {limit_gb:.0f} GB limit")
    return normalized


def build_key(
    user_id: uuid.UUID, meeting_id: uuid.UUID, *, content_type: str, filename: str | None
) -> str:
    """Where an object lives: `meetings/{user}/{meeting}/{random}{ext}`.

    The random component is the security-relevant part — a key that could be
    derived from ids a user already knows would be guessable, and presigned
    reads are the only thing standing between one tenant's media and another's.
    It also means a retried upload never collides with the attempt it replaces.

    The original filename is deliberately not in the path. It arrives from the
    client, so putting it in a key means sanitizing attacker-controlled text
    into a storage path; the extension carries everything we actually wanted
    from it.
    """
    extension = _EXTENSIONS.get(content_type, "")
    if not extension and filename:
        # Fall back to the client's own extension, but only if it is short and
        # plainly alphanumeric — anything else is not an extension.
        tail = filename.rsplit(".", 1)
        if len(tail) == 2 and 1 <= len(tail[1]) <= 5 and tail[1].isalnum():
            extension = f".{_UNSAFE.sub('', tail[1]).lower()}"
    return f"meetings/{user_id}/{meeting_id}/{uuid.uuid4().hex}{extension}"


async def reserve(
    db: AsyncSession,
    meeting: Meeting,
    *,
    content_type: str,
    size_bytes: int,
    filename: str | None = None,
):
    """Claim a key for `meeting` and hand back permission to upload to it.

    The row is written before the URL is returned, so an upload that starts is
    always attributable to a meeting — the alternative leaves objects in the
    bucket with nothing pointing at them.
    """
    normalized = validate(content_type, size_bytes)
    key = build_key(
        meeting.user_id, meeting.id, content_type=normalized, filename=filename
    )
    presigned = await run_in_threadpool(
        get_storage().presign_put,
        key,
        content_type=normalized,
        max_bytes=size_bytes,
    )
    meeting.media_key = key
    await db.flush()
    return presigned


async def confirm(db: AsyncSession, meeting: Meeting) -> int:
    """Verify the object landed, and move the meeting on to transcription.

    Returns the stored size. Raises `MediaRejected` when the bucket has nothing
    under the key, which is the case that matters: a client reporting success
    is not evidence of an upload, and taking its word queues a transcription
    job against an object that may never have been written.
    """
    if not meeting.media_key:
        raise MediaRejected("No upload was started for this meeting")

    stored = await run_in_threadpool(get_storage().head, meeting.media_key)
    if stored is None or stored.size_bytes <= 0:
        raise MediaRejected("That upload didn't finish — the file isn't there")
    if stored.size_bytes > settings.MEDIA_UPLOAD_MAX_BYTES:
        raise MediaRejected("That upload is larger than the limit")

    meeting.media_confirmed_at = datetime.now(timezone.utc)
    meeting.status = STATUS_RECORDED
    meeting.status_detail = None
    await db.flush()

    log.info(
        "meetings.media_confirmed",
        meeting_id=str(meeting.id),
        size_bytes=stored.size_bytes,
        source=meeting.source,
    )
    return stored.size_bytes


async def discard(meeting: Meeting) -> bool:
    """Delete a meeting's object. Returns whether the bytes are now gone.

    Used by the abandoned-upload janitor and by retention pruning. Failure is
    reported rather than raised — both callers are sweeps processing many rows,
    and one unreachable object must not stop the rest — but it *is* reported,
    because the caller's next move is to forget the key. Forgetting a key whose
    object is still there orphans it permanently: nothing would ever again know
    what to delete. A False here means "leave the key alone and try next sweep".
    """
    if not meeting.media_key:
        return True
    try:
        await run_in_threadpool(get_storage().delete, meeting.media_key)
    except Exception:
        log.exception("meetings.media_delete_failed", meeting_id=str(meeting.id))
        return False
    return True
