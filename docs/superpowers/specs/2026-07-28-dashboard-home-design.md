# Dashboard home — design

Date: 2026-07-28
Status: approved, ready for implementation planning

## Goal

Replace the placeholder `/dashboard` home page with the layout in the reference
screenshot, backed by real API data. Today the page renders lifetime-value
numbers as em-dashes, decides whether the inbox is "set up" from a localStorage
flag driven by a fake four-step timer, and hardcodes "No meetings scheduled".

The page after this work: a greeting hero with the ask bar, a card reporting how
much mail has actually been categorized and how many drafts written, and a
two-column Today/Tomorrow agenda whose per-meeting notetaker toggle really books
and recalls the bot.

Two repositories are involved:

- `InboxPilot` — FastAPI backend (this repo)
- `inboxos-web` — Next.js 14 frontend, sibling directory

## Scope

In scope:

- Persisted counters for emails categorized and drafts created
- Server-derived setup state, replacing the localStorage flag
- Today/Tomorrow agenda read from the user's calendar, with live bot state
- A per-meeting notetaker toggle that books and recalls bots
- The full page rebuild in `inboxos-web`

Out of scope, and deliberately presentational:

- **Subscribe for full access** — no billing exists in the backend. Static card
  linking to `/#pricing`.
- **Invite team** — no team or organization model exists. Static banner,
  dismissed to localStorage.
- **The ask bar's `+` attach and `Balanced` mode selector** — `AskRequest` is
  `conversation_id` + `message` only. Both controls are omitted rather than
  shipped inert; attachments and answer modes are separate features.

No automated tests are written as part of this work (see "Verification").

## 1. Data model

### 1.1 New table `activity_events`

New module `src/models/activity.py`, following `models/categorization.py` for
structure: module docstring, string constants for the enum-like column, then the
model.

```
activity_events
  id            uuid          pk                       (UUIDMixin)
  user_id       uuid          FK users.id ON DELETE CASCADE, indexed
  kind          varchar(32)   indexed
  ref_id        varchar(128)
  category_key  varchar(64)   nullable
  occurred_at   timestamptz   indexed, default now()
  created_at    timestamptz                            (TimestampMixin)
  updated_at    timestamptz                            (TimestampMixin)

  UNIQUE (user_id, kind, ref_id)  -- uq_activity_events_user_kind_ref
```

Constants in the same module:

```python
KIND_EMAIL_CATEGORIZED = "email_categorized"
KIND_DRAFT_CREATED = "draft_created"
KINDS = frozenset({KIND_EMAIL_CATEGORIZED, KIND_DRAFT_CREATED})
```

`ref_id` holds whichever Gmail id makes the event unique: the categorized
message id for `email_categorized`, and the *source* message id — the email
being replied to — for `draft_created`. The source message is the better key
than the draft id, because one source email should count once even if a re-run
causes Gmail to hold two draft objects for it.

The unique constraint is load-bearing, not hygiene. `classify_new_email` is
declared `autoretry_for=(Exception,), max_retries=3`, and `sync_last_7_days` is
documented as safe to re-run as the manual catch-up lever. Without the
constraint, one retry storm or one catch-up run permanently inflates the number
the user reads on the dashboard. All writes therefore use
`INSERT ... ON CONFLICT DO NOTHING` via
`sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(...)`.

`category_key` is not a foreign key, for the same reason
`CategorizationRule.category_key` is not one: `EmailCategory.key` is unique only
per user, so a reference would need a composite `(user_id, key)` target. Unlike
the rules table it is also never cleaned up when a category is deleted — these
rows are a historical record of what happened, and rewriting history to match a
renamed taxonomy would be wrong.

### 1.2 New column `users.initial_sync_at`

`initial_sync_at: Mapped[datetime | None]`, `DateTime(timezone=True)`, nullable,
added to `models/users.py`. Stamped once by `sync_last_7_days` when it completes.
It is the sole signal distinguishing "still onboarding" from "ready".

### 1.3 Migration

One Alembic revision creating `activity_events` (with its index set and unique
constraint) and adding `users.initial_sync_at`. Generated with
`make revision m="add activity events and initial sync marker"`, then read and
corrected by hand — autogenerate is a starting point here, not the output.

## 2. Recording activity

New module `src/services/activity/record.py`. Both functions are synchronous,
because every caller is Celery code, and both are idempotent.

```python
def record_email_categorized(user_id: str, message_id: str, category_key: str) -> None
def record_draft_created(user_id: str, source_message_id: str) -> None
```

Each wraps a private async insert with
`run_async(with_worker_session(...))` — the exact pattern
`services/categorization/pipeline.get_config` already uses at line 95, so
Celery's sync context is bridged the same way it is everywhere else in this
codebase.

Neither function may raise into its caller. A failure to record a statistic must
never fail the categorization it describes, nor trigger a Celery retry that
re-labels mail. Both catch broadly and `log.exception(...)`, mirroring how
`sync_last_7_days` guards `ensure_labels` and `get_config`.

### 2.1 Call sites

**Categorization** — `services/categorization/pipeline.categorize_and_apply`,
immediately after `gmail_ops.apply_category(...)` returns and beside the existing
`log.info("categorize.applied", ...)`. Recording after the label lands means a
Gmail failure never inflates the count. Every early `return None` path (disabled,
excluded by rule, missing category, below threshold with no fallback) records
nothing, which is correct — no label was applied.

**Drafts** — `services/digest/scheduling.py`, the single call site of
`integrations.composio.gmail.create_draft` in the codebase. The call sits in a
`try` block alongside `gmail_ops.add_label`, and `drafted += 1` follows it;
recording goes on that success path, keyed by the source message id `e.id`.

Recording lives at the call site rather than inside `create_draft` because
integrations may not import from services — the constraint is stated explicitly
in the `models/categorization.py` docstring and holds here.

Note that `create_draft` returns the draft id or `None` and the current code
discards it. Nothing in this design needs that return value, since `ref_id` is
the source message id, so the call site is left as it is apart from the added
recording.

If auto-reply drafts are built later — `workers/jobs/reply_draft_job.py` exists
but is an empty file, and the frontend `/dashboard/drafts` page configures a
feature with no backend — that job records through the same function, and the
counter starts reflecting it with no further change here.

**Sync completion** — `workers/jobs/sync_last_7_days.py` stamps
`users.initial_sync_at` at the end of the task, after `_install_trigger`, guarded
the same way as the other DB work in that task.

## 3. Setup state

Derived from the database alone. Two states:

- `syncing` — `users.initial_sync_at is null`
- `ready` — otherwise

The endpoint makes no Composio call to check whether Gmail is connected.
`DashboardLayout` already calls `checkAccess()`, which requires both Gmail and
Calendar to be connected and redirects to `/onboarding/connect` otherwise, so a
request that reaches this endpoint has already established connectivity.
Re-verifying would add a blocking third-party round trip per page load to
restate a known fact.

Consequence to accept knowingly: if a user revokes the Gmail grant while sitting
on the dashboard, the summary keeps reporting `ready` until they navigate and
`checkAccess()` runs again. That is the same staleness window every other
dashboard page already has.

## 4. The agenda

### 4.1 Why the calendar, not the meetings table

The `meetings` table holds only calls the sweep decided to book. The reference
screenshot shows `deep work (no calls please)` present in the day with its bot
**Off** — an event the sweep deliberately skips, which therefore has no row.
Rendering the agenda from `meetings` alone can never produce that row.

The agenda is therefore read from `calendar.list_events(user_id, time_min,
time_max)` — already used by `meetings_sweep` — and left-joined against
`meetings` on `calendar_event_id` to attach bot state.

Cost: one blocking Composio calendar call per dashboard load, wrapped in
`run_in_threadpool`, as `api/v1/meetings.py` already does for provider calls.
This is the same call `meetings.sweep` makes every minute for every auto-join
user, so its cost and failure modes are known.

### 4.2 Bucketing

The timezone comes from `MailmanSettings.timezone` via
`services.mailman.store.get_or_create_settings`, falling back to `UTC` when
absent. Day boundaries are computed server-side with `zoneinfo.ZoneInfo`:
local midnight today through local midnight the day after tomorrow, converted to
UTC for both the calendar query window and the bucket split.

The browser is never asked what day it is. A user in `Asia/Kolkata` loading the
page from a laptop still set to `UTC` must see the same two columns the backend
would compute.

Events without a `dateTime` start — all-day entries — are skipped, matching the
existing behaviour of `rules.event_bounds`, which returns `None` for them.
Within each bucket, events are ordered by start time ascending.

### 4.3 Bot state per row

For each calendar event, the matching `Meeting` row (by `user_id` +
`calendar_event_id`) yields two booleans:

- **`bot_on`** — `False` when no `Meeting` row exists, or its status is
  `cancelled` or `failed`. `True` otherwise.
- **`bot_editable`** — `False` once the call is underway or over: status in
  `recording`, `ended`, `recorded`, `processed`, `delivered`, or `starts_at` is
  in the past. `True` otherwise.

The UI disables the toggle when `bot_editable` is false rather than presenting a
control that would 409.

A row with no meeting link at all (`link_from_event` returns `None`) still
appears in the agenda — it is part of the user's day — with `bot_on: false` and
`bot_editable: false`, since there is nothing for a bot to join.

## 5. API

### 5.1 `GET /v1/dashboard/summary`

New router `src/api/v1/dashboard.py`, registered in `src/api/router.py`
alongside the others. Schemas in `src/schemas/dashboard.py`. Assembly logic in
`src/services/dashboard/summary.py`, keeping the router thin as the other v1
routers do.

```json
{
  "user": { "first_name": "Nilesh" },
  "setup": {
    "state": "ready",
    "initial_sync_at": "2026-07-26T09:12:00Z"
  },
  "stats": {
    "emails_categorized": 217,
    "drafts_created": 3
  },
  "meetings": {
    "timezone": "Asia/Kolkata",
    "today": [
      {
        "calendar_event_id": "abc123",
        "meeting_id": "0f6a755b-983b-442c-a4e6-ab03f918937f",
        "title": "Daily stand up - morning",
        "starts_at": "2026-07-28T06:30:00Z",
        "ends_at": "2026-07-28T07:00:00Z",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "bot_on": true,
        "bot_editable": true
      }
    ],
    "tomorrow": []
  }
}
```

`first_name` is the first whitespace-separated token of `User.full_name`,
falling back to the local part of `User.email` when `full_name` is null.

Statistics come from a single grouped query:

```sql
SELECT kind, count(*) FROM activity_events WHERE user_id = :uid GROUP BY kind
```

Lifetime totals, per the agreed window — no date filtering. Kinds absent from the
result report `0`.

Calendar failure is not fatal to the page. If `list_events` raises, the endpoint
logs and returns `today` and `tomorrow` as empty arrays with the rest of the
payload intact; the stats card must not disappear because a third party is down.

### 5.2 `POST /v1/meetings/bot`

New endpoint in the existing `src/api/v1/meetings.py`. Turns the notetaker **on**
for a calendar event, covering both "never booked" and "previously cancelled"
through one path.

Request: `{"calendar_event_id": "abc123"}`
Response: `MeetingRead`, `202 Accepted`

Behaviour, mirroring the existing `POST /meetings/join`:

1. `409` if `MeetingSettings.enabled` is false — the same guard `join_meeting`
   applies.
2. Fetch the event from the calendar and `409` if it is not found in the
   read window, or if `link_from_event` finds no joinable link.
3. `store.upsert_from_event(db, user.id, event)` — the existing helper, which
   already refreshes title and times on an existing row.
4. `409` if the resulting row's status is `recording`, `ended`, `recorded`,
   `processed`, or `delivered`, or its `starts_at` is in the past. This is
   exactly the `bot_editable is False` condition from §4.3, and the two must be
   kept in step — the endpoint is the authority, and the flag exists so the UI
   can avoid provoking this 409.
5. Reset status to `pending`, clear `status_detail`, dispatch
   `join_now.delay(str(meeting.id))`.

Turning the notetaker **off** needs no new endpoint: the existing
`DELETE /v1/meetings/{meeting_id}/bot` already recalls the bot and sets
`cancelled`. The frontend calls it whenever `meeting_id` is present.

## 6. Frontend

Repository: `inboxos-web`.

### 6.1 New `src/lib/dashboard.ts`

TypeScript types mirroring the payload above, plus:

```ts
getDashboardSummary(): Promise<DashboardSummary>
enableMeetingBot(calendarEventId: string): Promise<void>
disableMeetingBot(meetingId: string): Promise<void>
```

All three go through the existing `apiFetch`, which already unwraps FastAPI's
`detail` field into readable messages.

### 6.2 `src/app/dashboard/page.tsx`

Rewritten around a single fetch in `useEffect`, with three render states:
skeleton while loading, an error card with a retry button on failure, and the
page proper.

Removed as part of this: the `SetupView` / `MatureView` split, the four-step
`setTimeout` animation, the `AnalyticsCard` trio (superseded by the stats card),
and the hardcoded scheduling-link card. `isSetupDone` and `setSetupDone` are
deleted from `src/lib/auth.ts`, along with the `SETUP` key handling in
`signOut` and `resetOnboarding`.

Page order, top to bottom: greeting hero with `AskBar`, `InboxSetupCard`,
`SubscribeBanner`, `InviteTeamBanner`, then the meetings section.

### 6.3 Components

All new components live under `src/components/app/` and use the existing Tailwind
tokens (`cream`, `card`, `ink`, `accent`, `accent-dark`) — no new colours.

- **`InboxSetupCard`** — split card. Cream left panel holding the existing
  `ProgressRing`; right side with icon-prefixed stat rows and an
  `Open Gmail →` button linking to `https://mail.google.com`. When `setup.state`
  is `syncing`, the ring shows indeterminate progress and the heading reads
  "Setting up your inbox"; when `ready`, a completion check and "Your inbox is
  set up".
- **`MeetingsPanel`** — replaces the current hardcoded version. Takes the two
  arrays and the timezone, renders two column cards, and shows
  "No meetings scheduled" per column when empty.
- **`MeetingRow`** — title, `HH:MM - HH:MM` time range formatted in the payload's
  timezone, bot pill toggle, and an external-link button to `meeting_url`.
  Toggling optimistically flips the pill, calls enable or disable, and reverts
  with a toast on failure. Disabled when `bot_editable` is false.
- **`SubscribeBanner`** — static, links to `/#pricing`.
- **`InviteTeamBanner`** — static, dismissed to a localStorage key.

`AskBar` keeps its current props; only its styling is adjusted to the
screenshot's proportions. Submitting still routes to `/dashboard/chat`.

The greeting reads `{Morning|Afternoon|Evening}, {first_name}. Anything you'd
like to know?`, with the period chosen from the browser clock — a cosmetic
choice where local time is the right source, unlike day bucketing.

## 7. Verification

No automated tests are written for this work, by explicit decision. The repo has
no test suite today: `pyproject.toml` declares `testpaths = ["tests"]` but no
`tests/` directory exists, so there is no fixture harness to build on, and
standing one up is not a side effect this change should carry.

Verification is manual, against a running stack (`make up`, `make migrate`):

1. `make lint` and `make fmt` clean; frontend `npm run lint` and
   `npm run build` clean.
2. `GET /v1/dashboard/summary` via `/docs` returns the documented shape.
3. Counters increase by exactly one per newly categorized message, and re-running
   `sync_last_7_days` does not change the total.
4. A user whose `initial_sync_at` is null renders the syncing card; stamping it
   renders the set-up card.
5. Meetings land in the correct column for a non-UTC `MailmanSettings.timezone`,
   including an event shortly after local midnight.
6. Toggling a row off recalls the bot and persists across reload; toggling it
   back on re-books it.

## 8. File manifest

Backend (`InboxPilot`):

| Path | Change |
| --- | --- |
| `src/models/activity.py` | new — `ActivityEvent`, kind constants |
| `src/models/users.py` | add `initial_sync_at` |
| `alembic/versions/*.py` | new — table + column |
| `src/services/activity/record.py` | new — idempotent recording |
| `src/services/dashboard/summary.py` | new — assembly, bucketing, bot state |
| `src/schemas/dashboard.py` | new — response schemas |
| `src/api/v1/dashboard.py` | new — `GET /dashboard/summary` |
| `src/api/router.py` | register the dashboard router |
| `src/api/v1/meetings.py` | add `POST /meetings/bot` |
| `src/services/categorization/pipeline.py` | record on successful label |
| `src/services/digest/scheduling.py` | record on draft created |
| `src/workers/jobs/sync_last_7_days.py` | stamp `initial_sync_at` |

Frontend (`inboxos-web`):

| Path | Change |
| --- | --- |
| `src/lib/dashboard.ts` | new — types and calls |
| `src/app/dashboard/page.tsx` | rewritten |
| `src/lib/auth.ts` | drop `isSetupDone` / `setSetupDone` |
| `src/components/app/InboxSetupCard.tsx` | new |
| `src/components/app/MeetingsPanel.tsx` | rewritten |
| `src/components/app/MeetingRow.tsx` | new |
| `src/components/app/SubscribeBanner.tsx` | new |
| `src/components/app/InviteTeamBanner.tsx` | new |
| `src/components/app/AskBar.tsx` | restyled |
| `src/components/app/icons.tsx` | add icons used by the new cards |
