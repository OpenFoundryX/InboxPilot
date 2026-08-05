# Meeting Capture — Invite, Record, Upload

**Date:** 2026-08-05
**Status:** Draft, for engineering review
**Repos:** `InboxPilot` (API, workers), `inboxos-web` (UI)

---

## Goal

Give the Notetaker three ways to capture a meeting instead of one. Today a call is
only recorded if a Recall bot joins it from the user's calendar. After this, the
"Record meeting" menu offers:

| Menu item | What it does |
|---|---|
| **Invite to meeting** | Paste a Zoom/Meet/Teams link; a bot joins that call now. |
| **Start recording now** | Record from the browser microphone — an in-person meeting, or a desk note. |
| **Upload recording** | Upload an existing audio or video file and get a transcript and summary. |

All three end in the same place: a `Meeting` row with a transcript, a summary,
action items, reminders, and a recap email.

## What already exists

Read before implementing. This design is mostly wiring, not invention.

| Existing | Reused as |
|---|---|
| `POST /v1/meetings/join` (`api/v1/meetings.py:106`) | **The entire "Invite to meeting" backend.** It already accepts a pasted invitation, extracts the link, checks quota, and dispatches a bot. Only the UI is missing. |
| `services/meetings/links.py` | Link extraction. Unchanged — Zoom, Meet, and Teams stay the supported set. |
| `integrations/meetingbot/base.py` | The **template** for the new storage boundary: `Protocol` + frozen dataclasses + `get_provider()` from a settings string. |
| `services/meetings/recording.py` | Presigned-link resolution with caching, a freshness margin, and a prune guard. Gains a second branch rather than a second implementation. |
| `workers/jobs/process_meeting.py` | Its second half (summarize → reminders → recap) becomes shared. |
| `services/billing/entitlements.py` | `FEATURE_MEETING_BOT` gates all three paths. `usage.add_bot_seconds` meters all three. |
| `services/meetings/summarize.py` | Summarization. Gains one parameter (§7). |
| `RecordMeetingMenu.tsx` | Already renders all three items, two disabled with the reason. This enables them. |
| `RecordingPlayer.tsx`, `MeetingVideo.tsx` | Playback. Work as-is once a URL resolves. |

## Decisions

Settled during design. Each closes an option rather than deferring it.

| Decision | Rationale |
|---|---|
| **S3/R2 for media** | Uploads and browser recordings need playback, so the bytes must be kept. Presigned links with an expiry is the pattern `recording_url` already implements for Recall. |
| **`gpt-4o-transcribe` for non-bot media** | `OPENAI_API_KEY` and the `openai` SDK are already wired for summarization. No new vendor, key, or billing relationship. |
| **Transcribe after stopping, not live** | The transcript appears seconds after Stop. Avoids WebSocket infrastructure, a second transcription path, and per-minute cost while nobody speaks. |
| **Microphone only** | One permission prompt, works in every browser. Tab/system audio is a second scarier prompt and is Chrome-only. |
| **Zoom, Meet, Teams only** | Webex, Slack huddles, and GoTo are a link-matcher change with no user asking for it yet. |
| **Meter against the existing bot-hour cap** | One number the user understands and one place to enforce it. A separate allowance would mean new plan fields, new usage rows, and a second number in the UI. |
| **A browser recording is an upload** | Both end as an audio object in our bucket awaiting transcription. One storage boundary, one worker job, one pair of endpoints. The recorder is a frontend concern. |
| **No automated tests** | Explicit instruction from the project owner. See §12. |

## Non-goals

- **The shared/private live Notes pane.** A collaborative document with its own
  persistence, sharing, and concurrency model. Unrelated to getting audio in.
- **Live streaming transcripts.** Superseded by the decision above.
- **Speaker diarization on non-bot media.** `gpt-4o-transcribe` does not return
  speaker labels. See §7 for how this is handled rather than hidden.
- **Resuming a recording whose tab crashed.** See §6.

---

## Architecture

### 1. Storage boundary

New `src/integrations/storage/`, mirroring `integrations/meetingbot/` exactly:

```
src/integrations/storage/
  __init__.py   # get_storage() from MEDIA_STORAGE_PROVIDER
  base.py       # Protocol + frozen dataclasses + StorageError
  s3.py         # boto3, S3 and R2 by endpoint URL
```

Surface, and nothing more:

| Method | Purpose |
|---|---|
| `presign_put(key, content_type, max_bytes)` | A URL the browser can PUT to directly. |
| `presign_get(key)` | A playback URL plus its `expires_at`. |
| `head(key)` | Size and content type, or `None` when the object is absent. Confirms an upload landed. |
| `delete(key)` | Retention pruning. Idempotent. |

Nothing above `integrations/` learns which vendor is in play, matching the
meeting-bot boundary. Calls are blocking: call them from Celery workers, or via
`run_in_threadpool` from the API.

**The browser PUTs straight to S3.** A 1 GB body must never pass through FastAPI —
it would pin a worker for minutes and double egress. This requires a CORS rule on
the bucket allowing `PUT` from the web origin (§11).

### 2. Data model

`Meeting` already models a call's whole life. Uploads are two new `source` values,
not a new table.

```python
SOURCE_UPLOAD = "upload"   # a file the user gave us
SOURCE_LIVE = "live"       # recorded in the browser
```

Column changes on `meetings`:

| Column | Change | Why |
|---|---|---|
| `media_key` | **New**, `String(512)`, nullable | The S3 object key. Also the discriminator: `media_key` set → our storage; `bot_id` set → Recall. |
| `media_confirmed_at` | **New**, `DateTime(tz)`, nullable | Set when `head()` confirms the object landed. Distinguishes "awaiting bytes" from "has bytes" — the janitor in §8 depends on it. |
| `meeting_url` | **Nullable** (was `NOT NULL`) | An uploaded file has no URL to join. |

`recording_url`, `recording_url_expires_at`, and `recording_pruned_at` are reused
untouched. A short-lived signed link with a known deadline is exactly what they
were built for.

`has_recording` becomes
`bool(self.recording_id or self.recording_url or self.media_confirmed_at)` —
gated on the confirmation, not on `media_key`. A row that reserved a key but never
received bytes has no recording, and badging it as playable in the list would offer
a Watch button that resolves to nothing.

### 3. Resolving a playback URL

`resolve_recording_url` (`services/meetings/recording.py:30`) becomes a two-branch
dispatcher. Its prune guard, cache-freshness check, and `FRESHNESS_MARGIN` apply to
both branches unchanged:

```
recording_pruned_at set  → None                        (unchanged)
cached URL still fresh   → the cached URL              (unchanged)
media_confirmed_at set   → storage.presign_get(key)    (new)
bot_id set               → provider.fetch_recording()  (unchanged)
otherwise                → None
```

The new branch keys on `media_confirmed_at`, not on `media_key`: presigning a key
whose bytes never arrived hands the player a URL that 404s. An in-progress live
recording has a key and no confirmation, and correctly resolves to nothing.

The `MEDIA_READY_STATUSES` gate stays on the Recall branch only. Our own media is
playable the moment it is confirmed, before any transcript exists — a user who just
stopped recording should be able to play it back immediately, while transcription
is still queued.

### 4. The shared tail

`process_meeting._process` currently runs *fetch transcript → meter → summarize →
reminders → recap* in one function. The new job needs the second half verbatim.

Extract `services/meetings/pipeline.py`:

```python
async def finalize(db, meeting: Meeting) -> dict:
    """From a stored transcript to a delivered recap."""
```

It owns everything from `summarize()` onward: summary, decisions, action items,
`STATUS_PROCESSED`, reminder creation, the recap email, `STATUS_DELIVERED`. Both
jobs then read as *get a transcript, meter it, finalize*.

Without this the two paths drift, and a recap from an upload quietly stops matching
one from a bot. `process_meeting` keeps its retention stamping, empty-transcript
early return, and `MeetingBotError` retry — those are bot-specific.

### 5. Upload flow

Three steps, because a 1 GB body cannot be one.

**1. `POST /v1/meetings/uploads`**

```json
{ "filename": "standup.mp4", "content_type": "video/mp4", "size_bytes": 734003200,
  "title": "Standup", "calendar_event_id": null }
```

Validates the content type against an allowlist and the size against
`MEDIA_UPLOAD_MAX_BYTES`, checks `FEATURE_MEETING_BOT` (402 when over cap, as
`join` and `enable_bot` already do), creates the row with
`status=pending, source=upload, media_key=<generated>`, and returns:

```json
{ "meeting_id": "...", "upload_url": "https://...", "expires_at": "..." }
```

`calendar_event_id` is the "Link to event" dropdown. When given, the upload attaches
to the existing row for that event via `upsert_from_event` — the file is a recording
*of* that meeting — inheriting its title, attendees, and times. When null, the row is
standalone and `starts_at` is the upload time.

Keys are `meetings/{user_id}/{meeting_id}/{uuid4}{ext}`. The random component means a
guessed key is useless and a retried upload never collides.

**2. The browser PUTs to the presigned URL**, showing progress.

**3. `POST /v1/meetings/{id}/uploads/complete`**

`head()`s the object. Absent, or larger than declared → 409 and the row stays
`pending` for the janitor. Otherwise sets `media_confirmed_at`, moves to
`STATUS_RECORDED`, and enqueues `meetings.transcribe`.

The client never reports success — only the server's own `head()` does. A client
that claims an upload it never made would otherwise queue a job against a
nonexistent object.

### 6. Live recording

**`POST /v1/meetings/live`** creates the row (`source=live`, `status=recording`,
`starts_at=now`, `title` optional), checks quota the same way, and returns the
`MeetingRead` plus the same `{upload_url, expires_at}` pair as §5 — so the UI can
navigate straight to the detail page with the **Live** badge, timer, and waveform,
already holding the target it will PUT to on Stop.

The content type is fixed at `audio/webm` and the size bound is
`MEDIA_UPLOAD_MAX_BYTES`, since neither is known when recording starts.

`MediaRecorder` records Opus into memory — two hours is roughly 30 MB. On Stop the
browser does the same PUT and the same `uploads/complete` as §5, and the row follows
the identical path from there.

**Accepted limitation:** a tab crash mid-recording loses the audio. Streaming chunks
to S3 as they are produced would fix it, at the cost of multipart upload state on
both sides. Not worth building until someone hits it — and the janitor below means a
crash leaves no debris.

### 7. Transcription

New `src/services/meetings/transcribe.py`, running in the worker, constructing its
`OpenAI` client per call like `summarize.py:50`:

1. Stream the object to a temp file via a presigned GET.
2. `ffmpeg` → 16 kHz mono MP3 at 32 kbps. Uploaded video is mostly pixels we are
   paying to move; this reduces an hour to roughly 14 MB.
3. `ffprobe` for the true duration. This is what meters — after the fact, from the
   media itself, exactly as a bot's duration is metered from the provider's payload.
4. Files over 20 MB split into 15-minute segments, transcribed separately and
   stitched with time offsets. OpenAI's per-request ceiling is 25 MB.
5. Return a `Transcript` of `TranscriptSegment`s — the same type `fetch_transcript`
   returns, so `finalize` cannot tell the two sources apart.

**Speaker labels are lost on this path.** Recall's bot transcripts are diarized;
`gpt-4o-transcribe` returns plain text. Segments carry `speaker=None`, and
`Transcript.render()` would prefix every line with `Unknown:`.

So `render()` omits the prefix entirely when no segment has a speaker, and
`summarize()` gains `speakers_labelled: bool = True`. When false, the prompt states
that the transcript is unattributed and that action-item owners must be left null
unless a name is spoken aloud. Otherwise the model invents owners from the attendee
list, and a recap that confidently assigns work to the wrong person is worse than one
that assigns none.

New worker job `workers/jobs/transcribe_media.py`, task `meetings.transcribe`:

```
load + guard (already has a transcript? already delivered? no media_key? → skip)
stamp retention windows          (same as process_meeting)
download → transcode → probe
meter duration via add_bot_seconds, guarded on duration_seconds is None
transcribe
empty transcript → STATUS_PROCESSED, "empty transcript", stop
finalize(db, meeting)
```

Metering is guarded on `duration_seconds is None` and happens before the
empty-transcript return, matching `process_meeting` exactly: a retry must not meter
twice, and silence still costs us money.

### 8. Abandoned uploads

A row created at step 1 whose bytes never arrive is a leak in waiting, as is a live
recording whose tab was closed. A janitor pass — folded into the existing
`meetings.sweep` beat job — fails rows where `media_key` is set,
`media_confirmed_at` is null, and `created_at` is over 24 hours old, deleting any
orphaned object first.

### 9. Retention

**The current prune would silently skip S3 media.** `retention_sweep.py:76` selects
rows with `recording_id.isnot(None)`. A meeting whose media lives under `media_key`
never matches, so its object would never be deleted and `recording_pruned_at` would
never be set — the per-plan video window would apply to bot recordings only, while
the code looked like it handled retention.

Two changes:

- The video query selects `or_(recording_id.isnot(None), media_key.isnot(None))`.
- When `media_key` is set, the prune calls `storage.delete(key)` and nulls
  `media_key` alongside the existing columns. Dropping a pointer is enough for
  Recall, which enforces its own retention; for our own bucket it is not.

A `StorageError` during delete is logged and the row is left for the next sweep —
nulling the key on a failed delete would orphan the object permanently.

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/meetings/uploads` | Reserve a row, get a presigned PUT URL. |
| `POST` | `/v1/meetings/{id}/uploads/complete` | Confirm the object landed; enqueue transcription. |
| `POST` | `/v1/meetings/live` | Start a browser recording; returns the row and an upload target. |
| `POST` | `/v1/meetings/join` | **Unchanged.** Already ships. |

`MeetingRead.meeting_url` becomes `str | None`.

## Frontend (`inboxos-web`)

| File | Change |
|---|---|
| `RecordMeetingMenu.tsx` | Enable all three items; delete the "aren't available yet" note. |
| `InviteToMeetingModal.tsx` | **New.** URL field → `POST /meetings/join` → navigate to the meeting. |
| `UploadRecordingModal.tsx` | **New.** Event picker, title, file input, progress bar; drives the three-step upload. |
| `LiveRecorder.tsx` | **New.** `getUserMedia`, `MediaRecorder`, `AnalyserNode` waveform, timer, Stop → upload. |
| `lib/meetings.ts` | `meeting_url: string \| null`; add the three API calls; map `source` for the Live badge. |

`meeting_url` going nullable costs one line. The UI already guards it at
`notetaker/[id]/page.tsx:144` and `MeetingRow.tsx:42`, and `lib/dashboard.ts:21`
already types it `string | null` — only `lib/meetings.ts:14` disagrees.

## Migration

One Alembic revision:

1. `ALTER TABLE meetings ALTER COLUMN meeting_url DROP NOT NULL`
2. `ADD COLUMN media_key VARCHAR(512)`
3. `ADD COLUMN media_confirmed_at TIMESTAMPTZ`

Additive and backward compatible. Existing rows keep a non-null `meeting_url` and a
null `media_key`, so every existing meeting takes the Recall branch exactly as before.

## Configuration

```
MEDIA_STORAGE_PROVIDER=s3
S3_ENDPOINT_URL=            # blank for AWS; the account endpoint for R2
S3_REGION=auto
S3_BUCKET=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
MEDIA_UPLOAD_MAX_BYTES=1073741824      # 1 GB, matching the UI copy
MEDIA_URL_TTL_SECONDS=3600
TRANSCRIBE_MODEL=gpt-4o-transcribe
```

Two infrastructure prerequisites:

- **`ffmpeg` in the Docker image.** The Dockerfile is `python:3.12-slim` with no
  `apt-get` layer. `ffmpeg` and `ffprobe` are on the developer's Mac via Homebrew,
  so without this the transcode works locally and fails in the container — the worst
  possible failure ordering. Add an `apt-get install -y --no-install-recommends
  ffmpeg` layer to the `base` stage.
- **`boto3` in `pyproject.toml`.**
- **A CORS rule on the bucket** allowing `PUT` and `GET` from the web origin, with
  `ETag` exposed.

## Testing

**No automated tests, by explicit instruction from the project owner.**

Recorded here because the repo's eleven test files establish a convention this work
would otherwise follow — `tests/services/meetings/test_links.py` is the nearest
neighbour. The pure logic added here (key derivation, chunk splitting, size and
type validation, the `render()` change when speakers are unlabelled) is the kind
that convention would cover.

Manual verification path: upload a short MP4 → confirm the row reaches `processed`
with a transcript and a summary → confirm playback resolves → record 30 seconds in
the browser → confirm the same → paste a Meet link → confirm a bot joins.

## Known gaps

- No diarization on uploaded or self-recorded media (§7). Handled honestly rather
  than hidden, but a bot transcript remains strictly better.
- A tab crash loses an in-progress browser recording (§6).
- Duration is only known after transcode, so an upload that pushes a user over their
  cap is metered rather than refused. This matches how a bot that overruns its cap
  mid-call already behaves.
- Mic-only capture means the far side of a video call is captured only through the
  speakers. Tab audio is the fix when someone asks for it.
