# Renaming and Deleting Meetings

**Date:** 2026-08-06
**Status:** Approved, for implementation
**Repos:** `InboxPilot` (API), `inboxos-web` (UI)

---

## Goal

A meeting can be captured four ways now — calendar, pasted link, browser recording,
upload — and none of them can be corrected or removed afterwards. A recording
titled "Meeting on Aug 5" stays that way forever, and a test recording lives in the
list and the bucket permanently.

Two operations: rename, and delete.

## Decisions

| Decision | Rationale |
|---|---|
| **One delete, not two** | A single Delete removing the row and its media together. A separate "delete the recording but keep the notes" is a second thing to explain for a case nobody has asked for. |
| **Hard delete, not archive** | Delete means gone. A soft-delete flag means every existing meetings query grows a `not deleted` filter, and rows accumulate forever — a lot of permanent complexity to make an undo possible. |
| **Deleting cancels an active bot** | Rather than refusing until the user cancels it themselves. The confirmation dialog covers a misclick; a 409 would force a two-step dance for the ordinary case. |
| **Metered usage is never refunded** | Bot-seconds already recorded stay recorded. Returning them would make delete-after-each-call an unlimited-usage loophole. |
| **Reminders survive** | They carry `origin=meeting` but no foreign key to one. A commitment you made does not stop existing because you deleted the recording of it. |
| **No automated tests** | Same standing instruction as the capture work. |

## API

### `PATCH /v1/meetings/{id}`

```json
{ "title": "Design review" }
```

Trimmed, 300 characters max. An empty string clears the title to null, where the
UI already falls back to `Meeting on 5 Aug 2026`. Returns the updated
`MeetingRead`. No status restriction — a meeting can be renamed at any point in
its life, including while it is recording.

### `DELETE /v1/meetings/{id}` → 204

Three things to clean up, in an order that matters:

**1. A bot still in the call.** If `bot_id` is set and the status is in
`ACTIVE_STATUSES`, recall it first. If the provider refuses, the request fails
**502 and the row survives.** A notetaker still sitting in a meeting the user just
deleted is a privacy problem; better they retry than that happen silently. A bot
that is merely `pending` or already finished needs no recall.

**2. The object in our bucket.** Removed with the same `media.discard()` retention
uses. If it fails, the delete stops with **503 and the row survives** — dropping
the row while the bytes remain orphans them permanently, since nothing would ever
again know the key. This is the same rule the retention sweep already follows.

**3. The row**, only once both have succeeded.

Ordering is deliberate: every step that can fail happens while the row is still
there to retry from.

### Not changed

`DELETE /v1/meetings/{id}/bot` keeps its existing meaning — recall the bot, keep
the meeting. Distinct path, no conflict.

## UI (`inboxos-web`)

Both surfaces get a `···` menu with Rename and Delete:

| Surface | Change |
|---|---|
| `MeetingListRow` | Has no menu today; gains one. |
| `notetaker/[id]/page.tsx` | Already has a `···` menu (open link, cancel notetaker); the two items join it, and a successful delete navigates back to the list. |
| `RenameMeetingModal` | **New.** `Modal` with the current title prefilled. |
| `DeleteMeetingDialog` | **New.** Names the meeting and states the recording goes with it. Destructive and irreversible, so never a bare one-click. |
| `lib/meetings.ts` | `renameMeeting`, `deleteMeeting`. |

**One guard:** the tab cannot delete the meeting it is currently recording into.
`LiveRecorder` holds an upload target for that row, and completing against a
deleted meeting would 404 and lose the audio. Delete is disabled for it.

## Known gaps

- No undo. Stated plainly in the confirmation rather than mitigated.
- Deleting a meeting whose recap email already went out does not unsend it. The
  summary in the user's inbox outlives the row, which is a property of email, not
  something this can fix.
