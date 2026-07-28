# Onboarding simplification

**Date:** 2026-07-28
**Repos:** `InboxPilot` (backend), `inboxos-web` (frontend)

## Problem

Onboarding is two disconnected flows.

The real one is `/onboarding/connect`: it connects Gmail and Google Calendar
through Composio and then drops the user on the dashboard. It asks nothing else,
so a new user lands on a dashboard where scheduled mail, categorization, and the
meeting notetaker are all off, with no prompt to turn any of them on.

The other is a mock: `/onboarding/creating` → `/calendar` → `/inbox` → `/notes`.
Nothing in it works. The calendar step calls `router.push` and connects nothing.
The inbox choice writes to `localStorage` under `inboxos_inbox_pref` and never
reaches the API. The "summarize my meetings" toggle is discarded when the user
clicks Finish. `/onboarding/creating` animates for a fixed 2.5 seconds while no
work happens.

Every endpoint the real steps need already exists in `InboxPilot`, and
`inboxos-web/src/lib/{mailman,categorization,meetings}.ts` already wraps all of
them. The work is wiring, not new API surface.

## Goal

One flow: connect the two accounts, then answer one question each about
scheduled mail, categorization, and the meeting bot. Delete the mock pages.

## Flow

```
/login → /onboarding/connect → /onboarding/mail → /onboarding/categories → /onboarding/notetaker → /dashboard
            Gmail + Calendar      schedule            labels                  meet bot
```

Deleted: `/onboarding/calendar`, `/onboarding/inbox`, `/onboarding/notes`, and
the standalone `/onboarding/creating` route.

`Orbit.tsx` survives, moved inline into the last step as an "Applying your
settings…" state shown while the finish calls run. It replaces a route that
animated over nothing with the same animation over real work.

`OnboardingStepper` lists the four real steps and renders on every step,
including `connect`. Today `connect` renders full-bleed with no stepper, so a
user cannot see they are on step 1 of 4.

Steps 2–4 each carry a "Skip for now" link that advances without writing
anything. All three features default to off in the backend, so a skip leaves
nothing half-configured. `connect` has no skip — nothing in the product works
without both grants.

## Step content

Each step asks exactly one question and writes defaults for the rest. Full
control stays on the dashboard pages that already exist.

### Step 2 — `/onboarding/mail`

Heading: "When should we deliver your email?"

| Choice | `PUT /v1/mailman/settings` |
| --- | --- |
| A few times a day (preselected) | `delivery_mode: "times"`, `times_per_day: 3` |
| Every 2 hours | `delivery_mode: "interval"`, `interval_hours: 2` |
| Keep mail arriving live | no write; batching is never activated |

The two writing choices also send `timezone` from
`Intl.DateTimeFormat().resolvedOptions().timeZone` rather than leaving it at the
backend default. Active window, DND, and VIP stay at backend defaults and are
editable at `/dashboard/mailman`.

"Keep mail arriving live" means step 4's finish handler skips
`POST /v1/mailman/start`. The settings row is left untouched.

### Step 3 — `/onboarding/categories`

Heading: "Choose what stays in your inbox" (the copy from the deleted mock
`/onboarding/inbox`, now wired to the API).

| Choice | Writes |
| --- | --- |
| Only what needs my attention (preselected) | `PUT /categorization/settings {is_enabled: true}`; `PATCH /categories/{key} {is_enabled: true}` for `to_do`, `to_follow_up`, `fyi`, `notification`; `PATCH {is_enabled: true, actions: {archive: true}}` for `marketing`, `noise` |
| All my emails | `is_enabled: true`; all six builtins enabled, no archive action |
| Don't label my emails | `PUT /categorization/settings {is_enabled: false}` |

The six builtin keys are fixed in `models/categorization.py`: `to_do`,
`to_follow_up`, `notification`, `fyi`, `marketing`, `noise`.

The archive action on `marketing` and `noise` is what makes the first option's
promise ("moves them out of your inbox") true.

### Step 4 — `/onboarding/notetaker`

Heading: "Should we join your meetings?"

| Choice | `PUT /v1/meetings/settings` |
| --- | --- |
| Only when I ask (preselected) | `enabled: true`, `auto_join: false` |
| Join every meeting automatically | `enabled: true`, `auto_join: true` |
| No thanks | `enabled: false` |

`auto_join` is deliberately not preselected. `models/meetings.py:83` records that
recording other people is the user's call to make deliberately, not something
onboarding switches on for them. This step keeps that property.

## When settings apply

Each step PUTs its own settings when the user clicks Continue. A resumed wizard
pre-fills from the corresponding GET, so no answer is lost.

The two calls with side effects outside our database fire only on Finish, from
step 4:

1. `POST /v1/mailman/start` — installs the skip-inbox Gmail filter. Running this
   at step 2 would mean a user who closed the tab gets a silent inbox with no
   dashboard to explain it.
2. `POST /v1/users/me/onboarding/complete`

No categorization backfill call is needed. `sync_last_7_days` is already queued
five seconds after the Gmail connect callback (`api/v1/integrations.py:96`) and
classifies the last 30 days on its own.

## Backend changes (`InboxPilot`)

1. `models/users.py` — add
   `onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)`.
2. Alembic migration — add the column, then
   `UPDATE users SET onboarded_at = now()`. Existing users, who already connected
   both accounts under the old flow, are treated as onboarded and are never
   pulled into the new wizard. They configure these features from the dashboard
   pages that already exist. Downgrade drops the column.
3. `schemas/user.py` — add `onboarded_at: datetime | None = None` to `UserRead`,
   which `GET /v1/auth/me` already returns.
4. `api/v1/users.py` (currently an empty router) —
   `POST /users/me/onboarding/complete`. Sets `onboarded_at` to now if it is
   null, leaves it alone if already set, returns `UserRead`. Idempotent, so a
   double-submit or a retry after a network blip is harmless.

## Frontend changes (`inboxos-web`)

- `lib/session.ts` — `checkAccess()` returns
  `{ authed, connected, onboarded }`. `connected` is both integration statuses;
  `onboarded` is `me.onboarded_at != null`. The current conflation of the two
  (onboarded means both connected) is what would otherwise let a user reach the
  dashboard without seeing steps 2–4.
- `app/onboarding/layout.tsx` — `!authed` → `/login`; `!connected` →
  `/onboarding/connect`; `onboarded` → `/dashboard`.
- Dashboard layout — `!onboarded` → `/onboarding/connect`.
- Resume always lands on step 2 rather than tracking per-step progress. Steps
  pre-fill from the API, so re-answering costs one click each and we avoid a
  second source of truth for where the user got to.
- `lib/auth.ts` — remove `isOnboarded`, `setOnboarded`, `resetOnboarding`, and
  the `inboxos_inbox_pref` key. The flag is real now. `signIn`/`signOut`/
  `isAuthed` stay for the no-backend mock path.
- New `lib/onboarding.ts` — `completeOnboarding()` wrapping the new endpoint.

## Errors

A failed settings PUT keeps the user on the step and shows an inline message,
matching the error card `connect` already uses.

A failed `POST /mailman/start` on Finish is non-fatal: onboarding is still marked
complete and the user still reaches the dashboard, where a banner reports that
batching could not be turned on. Trapping someone in a wizard over a Gmail API
hiccup is worse than an unbatched inbox they can fix from `/dashboard/mailman`.

A failed `POST /onboarding/complete` keeps the user on step 4 with a retry — it
is the one call that must land, or they re-enter the wizard on next login.

## Testing

Backend:

- `POST /users/me/onboarding/complete` sets `onboarded_at` when null.
- Calling it twice leaves the first timestamp unchanged.
- It requires auth (401 without a session).
- `GET /auth/me` includes `onboarded_at`.
- Migration upgrade backfills existing rows to a non-null value.

Frontend:

- Each step maps its selected choice to the documented request body.
- Skip advances without issuing a settings write.
- Finish calls `mailman/start` only when a batching choice was made, and always
  calls `onboarding/complete`.
- A `mailman/start` failure still routes to `/dashboard`.
- `checkAccess` gating: unauthed → login, authed but unconnected → connect,
  connected but not onboarded → step 2, onboarded → dashboard.

## Out of scope

- Outlook. The mock calendar step offered a "Continue with Outlook" button that
  did nothing; it disappears with that page. `User.outlook_sub` exists but no
  Outlook integration does.
- Redesigning the dashboard settings pages. They remain the place for full
  control over all three features.
- VIP configuration during onboarding. It stays at `/dashboard/mailman`.
