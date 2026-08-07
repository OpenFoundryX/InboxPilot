# Daily Notes

**Date:** 2026-08-06
**Status:** Approved, for implementation
**Repos:** `InboxPilot` (API), `inboxos-web` (UI)

---

## Goal

A scratchpad, one page per calendar day, scrolling in both directions from today.
Nothing in the product holds a thought that isn't attached to an email, a draft, or
a meeting; this is where the rest goes.

## Decisions

| Decision | Rationale |
|---|---|
| **The client names the date** | A daily note is "the page for August 6th", not the notes in some 24-hour window. The browser already knows what day it is locally, so the server stores a `Date` and does no timezone resolution — sidestepping the DST-length problem `dashboard.day_bounds` exists to handle. |
| **A window around today, both directions** | Past above, future below, today anchored. More work than a one-way feed, but it is the shape of a journal. |
| **Empty bodies are deleted, not stored** | Scrolling through a year would otherwise create 365 empty rows. A day with nothing written has no row, and reads as absent rather than as blank. |
| **Plain text** | Matches the reference. Markdown or rich text is a separate feature carrying its own editor decision. |
| **No integrations** | The digest, reminders, and action items stay out of it. A notes surface that also parses commitments is a second product. |
| **Past days stay editable** | Nothing freezes. Correcting yesterday is normal use. |
| **No automated tests** | Same standing instruction as the capture and delete work. |

## Data model

`models/notes.py` — a new domain, since nothing resembling notes exists today.

```python
class DailyNote(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "daily_notes"
    __table_args__ = (UniqueConstraint("user_id", "note_date"),)

    user_id: uuid.UUID          # FK users.id, CASCADE
    note_date: date             # a calendar date, not an instant
    body: str                   # plain text, never empty (empty rows are deleted)
```

`note_date` is a `Date` rather than a `DateTime` on purpose: the thing being
identified is a square on a calendar, and storing an instant would reintroduce the
question of whose midnight it is.

## API — `api/v1/notes.py`

### `GET /v1/notes?from=YYYY-MM-DD&to=YYYY-MM-DD`

Notes with content in the range, oldest first. Days with nothing written are simply
absent — the UI renders them as empty rather than the server inventing blanks.

The range is capped at **120 days**; wider is a 422. The client asks for the window
it is showing, so anything larger is a mistake rather than a need.

### `PUT /v1/notes/{date}`

```json
{ "body": "..." }
```

Upsert. **A blank body deletes the row** and returns it as empty. Without that,
scrolling through the page would create a row for every day merely visited.

Returns `{ date, body, updated_at }`.

## UI

New sidebar entry, **Daily**, and a page at `/dashboard/daily`.

**The window.** Starts at `today-7 … today+3`. Sentinels at both ends; the top one
extends 7 days into the past, the bottom 7 into the future.

**The scroll-anchoring problem.** Inserting days *above* the viewport pushes
everything down, so the page jumps out from under whoever triggered it. The top
extension captures `scrollHeight` before the insert and restores `scrollTop` by the
delta afterwards. This is the only genuinely fiddly part of the page.

**Each day** is a heading (`Thu, August 6th, 2026`, today carrying a dot) above an
auto-sizing textarea placeholdered `Write notes…`.

**Saving** is debounced ~800 ms after typing stops, and flushed on blur and on
`pagehide` — closing the tab mid-sentence is the ordinary way people leave a page
like this, and it must not cost the sentence.

| File | Change |
|---|---|
| `components/app/Sidebar.tsx` | A `Daily` entry in the nav array. |
| `app/dashboard/daily/page.tsx` | **New.** The window, the sentinels, the anchoring. |
| `components/daily/DayNote.tsx` | **New.** One day: heading, textarea, autosave. |
| `lib/notes.ts` | **New.** `getNotes(from, to)`, `saveNote(date, body)`, date helpers. |

## Known gaps

- No search across notes. With a date-keyed table it is a query away, but nothing
  asks for it yet.
- No offline buffering. A save that fails while the tab is closing is lost; the
  `pagehide` flush narrows that window rather than closing it.
- Concurrent edits in two tabs last-write-wins, with no merge.
