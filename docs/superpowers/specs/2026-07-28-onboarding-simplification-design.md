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
| A few times a day (preselected on a first visit) | `delivery_mode: "times"`, `times_per_day: 3` |
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
| Only what needs my attention (preselected until the saved state is read) | `PUT /categorization/settings {is_enabled: true}`; `PATCH /categories/{key} {is_enabled: true}` for `to_do`, `to_follow_up`, `fyi`, `notification`; `PATCH {is_enabled: true, actions: {archive: true}}` for `marketing`, `noise` |
| All my emails | `is_enabled: true`; all six builtins enabled, no archive action |
| Don't label my emails | `PUT /categorization/settings {is_enabled: false}` |

The six builtin keys are fixed in `models/categorization.py`: `to_do`,
`to_follow_up`, `notification`, `fyi`, `marketing`, `noise`.

The archive action on `marketing` and `noise` is what makes the first option's
promise ("moves them out of your inbox") true.

Continue calls `GET /categorization/categories` before the six PATCHes. That
endpoint seeds the built-ins, so the seeding happens once, in one request. Six
concurrent PATCHes against an unseeded account would otherwise each try to seed
in their own session and five would lose the `uq_email_categories_user_key`
race — which is exactly the state a brand-new user is in.

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
pre-fills so no answer is lost, but the source differs per step, because not
every answer is recoverable from a settings row:

- **Step 2** reads `inboxos_batching_choice` from `localStorage` first, and only
  falls back to `delivery_mode` from `GET /v1/mailman/settings`. "Keep mail
  arriving live" writes nothing, so `delivery_mode` still holds whatever was
  there before and can never reproduce that answer. The key is written on
  Continue and on Skip, cleared once `POST /onboarding/complete` succeeds, and
  cleared by `signOut()` — it must not outlive the wizard or the session.
- **Step 3** reads both `GET /categorization/settings` (`is_enabled: false` is
  "Don't label my emails") and `GET /categorization/categories`, where
  `marketing.actions.archive` is the only thing separating "Only what needs my
  attention" from "All my emails" — both enable all six labels. One consequence:
  a freshly seeded taxonomy is byte-identical to what "All my emails" writes, so
  on an account that has never answered, the step settles on "All my emails"
  once the read lands. That is an accurate picture of the account's current
  state, and it is the option that changes nothing.
- **Step 4** reads `GET /v1/meetings/settings` — `enabled` and `auto_join`
  distinguish all three answers.

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
- `app/onboarding/layout.tsx` — `!authed` → `/login`; then `!connected` →
  `/onboarding/connect` (rendering it if that is already the path); then
  `onboarded` → `/dashboard`. The order matters: `/onboarding/connect` is the
  only screen in the app with Gmail/Calendar connect buttons, so an onboarded
  user whose access was revoked has to be able to reach it. Checking `onboarded`
  first would bounce them to a dashboard where every call fails, with no way
  back. Connect's Continue goes to `/onboarding/mail`, which sends an onboarded
  user on to `/dashboard`, so the flow still terminates.
- Dashboard layout — `!onboarded` → `/onboarding/connect`.
- Resume always lands on step 2 rather than tracking per-step progress. Steps
  pre-fill from the API, so re-answering costs one click each and we avoid a
  second source of truth for where the user got to.
- `lib/auth.ts` — remove `resetOnboarding` and the `inboxos_inbox_pref` key.
  `isOnboarded`/`setOnboarded` stay: with no backend configured there is no
  `onboarded_at` to read, so the mock path still needs a local flag or the
  dashboard becomes unreachable. `signIn`/`signOut`/`isAuthed` stay for the same
  reason.
- New `lib/onboarding.ts` — `completeOnboarding()` wrapping the new endpoint.

### No-backend mock path

`backendConfigured()` is false when `NEXT_PUBLIC_API_URL` is unset, and then the
`/api` rewrite in `next.config.mjs` does not exist, so every `apiFetch` fails.
The deleted mock pages existed to give that path something to render.

The new steps cover it the way the rest of the app already does: each step falls
back to the `DEFAULT_SETTINGS` its `lib/` module already exports when the initial
GET fails, and Continue skips the write when `backendConfigured()` is false.
Finish calls `completeOnboarding()` when configured and `setOnboarded()` when
not. No mock-only pages remain.

## Deploy order

`InboxPilot` must be migrated and deployed before `inboxos-web` ships. The
frontend reads `onboarded_at` from `GET /v1/auth/me` and calls
`POST /v1/users/me/onboarding/complete`; against a backend without the migration
neither exists. `Boolean(undefined)` is `false`, so every existing user is pushed
into the wizard, and the Finish call 404s — trapping all of them behind a retry
that cannot succeed for as long as the gap lasts. In the other order there is no
gap: the column and the endpoint simply go unread until the frontend ships.

## Errors

A failed settings PUT keeps the user on the step and shows an inline message,
matching the error card `connect` already uses.

A failed `POST /mailman/start` on Finish is non-fatal: onboarding is still marked
complete and the user still reaches the dashboard, where a banner reports that
batching could not be turned on. Trapping someone in a wizard over a Gmail API
hiccup is worse than an unbatched inbox they can fix from `/dashboard/mailman`.

A failed `POST /onboarding/complete` keeps the user on step 4 with a retry — it
is the one call that must land, or they re-enter the wizard on next login.

## Verification

Neither repo has test infrastructure today — `inboxos-web` has no test runner and
`InboxPilot` has pytest configured with no `tests/` directory. Adding one is out
of scope for this change, so verification is static checks plus a manual walk.

Backend: `uv run ruff check src`, `uv run mypy src`, `make migrate` (then a
manual `alembic downgrade -1` / `upgrade head` round trip), and curling
`POST /v1/users/me/onboarding/complete` twice to confirm the timestamp does not
move on the second call.

Frontend: `npx tsc --noEmit`, `npm run lint`, `npm run build`.

Manual walk, against a running backend, with a user whose `onboarded_at` has been
set back to NULL:

1. `/dashboard` bounces to `/onboarding/connect`.
2. Connect both accounts; Continue lands on `/onboarding/mail`.
3. Each step's choice persists — reload mid-wizard and the selection is still
   there.
4. Skip on a step leaves that feature's settings untouched.
5. Finish reaches `/dashboard`; `GET /v1/auth/me` shows `onboarded_at` set and
   `GET /v1/mailman/settings` shows `is_active: true` unless "keep mail arriving
   live" was chosen.
6. Revisiting `/onboarding/mail` after finishing redirects to `/dashboard`.

## Out of scope

- Outlook. The mock calendar step offered a "Continue with Outlook" button that
  did nothing; it disappears with that page. `User.outlook_sub` exists but no
  Outlook integration does.
- Redesigning the dashboard settings pages. They remain the place for full
  control over all three features.
- VIP configuration during onboarding. It stays at `/dashboard/mailman`.
