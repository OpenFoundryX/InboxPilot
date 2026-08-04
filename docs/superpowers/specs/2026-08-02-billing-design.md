# Billing v1 — Starter and Pro

**Date:** 2026-08-02
**Status:** Approved, ready for implementation planning

## Goal

Charge for InboxPilot. Ship a working paywall for the two single-user tiers
advertised on the marketing site, enforce the limits those tiers promise, and
stop paying LLM and meeting-bot costs on accounts that are not paying.

## Scope

**In:** Razorpay subscriptions for Starter and Pro, a card-required 7-day trial,
entitlement enforcement across the API and the Celery workers, monthly usage
counters for bot-hours and AI drafts, and plan-driven retention pruning.

**Out, deliberately:**

- **Team and Enterprise.** Both are per-seat tiers with pooled quotas, shared
  mailboxes, and invites. There is no workspace, org, or team model in the
  codebase — `src/models/users.py` is a standalone user, and every table hangs
  off `user_id`. Those tiers need a workspace subsystem designed first. Separate
  project.
- **Overage billing.** Bot-hours and scheduling threads are advertised as
  metered with per-hour overage rates. This spec counts usage and hard-caps it;
  it does not report usage to Razorpay or invoice for it. The counters built here
  are the foundation for that later.
- **Vela scheduling entitlements.** Scheduling threads are a headline metered
  unit on Pro, but no Vela subsystem exists in `src/`. There is nothing to gate.
- **Add-ons.** All five (extra bot-hours, extra mailbox, SMS/WhatsApp, CRM sync,
  priority support) are deferred.

## Decisions

| Decision | Choice |
|---|---|
| Payment provider | Razorpay (serves India and international cards) |
| Currency | USD only for v1; the catalog carries a `currency` field so INR is additive |
| Trial | 7 days, card authorised up front, auto-converts |
| Trial mechanism | Razorpay subscription with `start_at = now + 7 days`; the `authenticated` state *is* the trial |
| Self-service | Cancel only. Card updates and plan changes go through support |
| Paywall position | After Gmail/Calendar connect, before dashboard |
| Entitlement source of truth | Python catalog in `src/core/plans.py` |
| Existing accounts | Get the same 7-day trial, granted by migration |
| Bot-hours | Counted and hard-capped, not billed |

The marketing site advertises a **14-day** Pro trial. This spec ships **7 days**.
`src/lib/plans.ts` and `src/components/app/TrialPill.tsx` in the `inboxos-web`
repo must be updated in the same release, or the site oversells the trial.

## Architecture

### Data model

New table `subscriptions`, one row per user:

| Column | Notes |
|---|---|
| `user_id` | unique FK to `users`, cascade delete |
| `razorpay_customer_id` | nullable — comped and pre-checkout users have none |
| `razorpay_subscription_id` | nullable |
| `plan_id` | `starter` \| `pro` |
| `interval` | `monthly` \| `annual` |
| `currency` | `USD` for v1. Present so INR plans are additive, not a migration |
| `status` | mirrors Razorpay: `created`, `authenticated`, `active`, `pending`, `halted`, `paused`, `cancelled`, `expired`, `completed` |
| `trial_ends_at` | nullable timestamp — mirrors the subscription's `start_at` |
| `current_period_end` | nullable timestamp |
| `cancel_at_period_end` | bool, default false |
| `comped` | bool, default false — escape hatch for design partners |

`trial_ends_at` has two writers: the subscription's `start_at` for users who
complete checkout, and the backfill migration for accounts that predate billing
and have no Razorpay customer. Both write the same field so every reader asks
one question rather than branching on account age.

Razorpay's state machine differs from Stripe's in a way that happens to suit us:
a subscription whose `start_at` is in the future sits in `authenticated` once the
customer has authorised the mandate but before the first charge. That state *is*
the trial — there is no separate trial flag to keep in sync.

New table `usage_counters`:

| Column | Notes |
|---|---|
| `user_id` | FK to `users`, cascade delete |
| `period_start` | first day of the billing month, UTC |
| `bot_seconds_used` | integer, default 0 |
| `drafts_generated` | integer, default 0 |

Unique on `(user_id, period_start)`. Rows are created lazily on the first write
of a period rather than reset by a scheduled job — a missed cron run cannot
hand out free quota, and a user who does nothing costs no rows.

New column `Meeting.duration_seconds`, populated on the transition into
`recorded` from the provider payload. Nothing in the current model records how
long a bot was in a call; `joined_at` exists but no counterpart. Without this
column bot-hours cannot be metered at all.

### Plan catalog

`src/core/plans.py` holds the enforced entitlements as frozen dataclasses.
Razorpay holds plan ids and prices and nothing else — no entitlement data lives
in the provider's notes or metadata, where a dashboard typo would silently
change what customers can do.

| Entitlement | Starter | Pro |
|---|---|---|
| Bot-hours per month | 5 | 15 |
| AI drafts per month | 20 | unlimited |
| Routines | `briefing` only | all four |
| Digests (invoice, deadline, newsletter) | off | on |
| Custom categories and rules | default categories only, no custom rules | full |
| Video retention | 7 days | 30 days |
| Transcript retention | 90 days | 1 year |

Two rows from the marketing matrix are intentionally absent:

- **Mailboxes (1 vs 2).** `src/api/v1/integrations.py` exposes a single
  `/gmail/connect` and `/gmail/status` per user through Composio. One mailbox
  per user is the only reachable state, so a limit of 1 or 2 can never be
  exceeded. Building the check would be dead code.
- **Everything under Vela.** No implementation exists to gate.

Prices: four Razorpay plan ids (starter monthly/annual, pro monthly/annual) in
config. Annual is billed as a yearly amount — $180 for Starter, $348 for Pro —
matching the $15 and $29 monthly-equivalent figures on the site.

### Entitlement enforcement

`src/services/billing/entitlements.py` exposes a plain function:

```
async def check(session, user, feature) -> Entitlement
```

It is deliberately **not** a FastAPI dependency. Meeting bots are booked by the
calendar sweep and drafts are generated by the auto-draft job — both in
`src/workers/jobs/`, neither behind an HTTP request. An enforcement layer that
only wraps API routes would leave the two most expensive operations in the
product ungated. API routes call it through a thin `Depends` wrapper that turns
a denial into `402 Payment Required`; workers call it directly and skip.

### Access states

| State | Razorpay status | Behaviour |
|---|---|---|
| Trialing | `authenticated` (mandate signed, first charge not yet due) | Entitlements of `plan_id` |
| Active | `active` | Entitlements of `plan_id` |
| Comped | any, with `comped = true` | Full Pro entitlements, no Razorpay record |
| Past due | `pending` (a charge failed, retries continuing) | Entitlements retained; banner warns |
| Locked | `created`, `halted`, `paused`, `cancelled`, `expired`, `completed`, or trial elapsed | Read-only |

`created` is locked deliberately: it means the subscription exists but the
customer never completed the mandate authorisation, so no card is on file.
`expired` is Razorpay's name for exactly that case once `start_at` has passed.

A trial is always a trial *of a specific plan*: the user picks Starter or Pro
before Checkout and trials that plan's limits, so nothing is silently withdrawn
on the day they convert.

Locked means: feature endpoints return `402`, and every worker sweep skips the
user before doing any paid work. Existing data stays readable so nobody loses
their mail history behind a paywall.

### Checkout and webhooks

Signup: Google login → connect Gmail and Calendar → pick plan → Razorpay Checkout
→ dashboard. The card ask lands after the user has watched the product get wired
to their mailbox.

Unlike Stripe, Razorpay Checkout is a **JavaScript modal, not a hosted redirect**.
The backend creates the subscription (`start_at = now + 7 days`) and returns its
id plus the public key; the browser opens the modal against that subscription and
the customer authorises the mandate there. Nothing sensitive crosses our servers,
but the frontend must load Razorpay's `checkout.js` from their CDN.

`POST /v1/webhooks/razorpay` verifies `X-Razorpay-Signature` — HMAC-SHA256 of the
**raw request body** keyed by `RAZORPAY_WEBHOOK_SECRET`. The body must be read
unparsed; re-serialising JSON before hashing produces a different digest and every
signature check fails. Events are deduped on the event id through the existing
`src/core/idempotency.py`. Handled events:

- `subscription.authenticated` — mandate signed; record ids, trial is running
- `subscription.activated` — first charge succeeded; move to `active`
- `subscription.charged` — renewal succeeded; advance `current_period_end`
- `subscription.pending` — a charge failed and is being retried
- `subscription.halted` — retries exhausted; lock
- `subscription.cancelled`, `subscription.completed`, `subscription.paused` — lock

Razorpay redelivers and does not guarantee order. Handlers are idempotent, and
each ignores an event carrying an older `current_period_end` than the row already
holds, so a late redelivery cannot resurrect stale state.

### Quota exhaustion

Running out of quota must never break mail flow. At the bot-hour cap, the
calendar sweep declines to book new bots and writes an `ActivityEvent` the
dashboard can surface; meetings still appear, just without a notetaker. At the
draft cap, the auto-draft job returns no draft and the mail is delivered
normally. Neither raises to the user as an error.

### Retention pruning

A beat-scheduled job prunes recordings past the plan's video window and
transcripts past its transcript window, clearing `recording_id`,
`recording_url`, and `transcript` while keeping the summary, decisions, and
action items — which are what the recap email already sent and what the user
actually returns to.

This is in scope because the windows are quoted in two places that are not
marketing copy: the pricing matrix and the privacy policy. Today nothing prunes
anything, so those windows are a stated policy the system does not honour.

## API surface

| Endpoint | Purpose |
|---|---|
| `GET /v1/billing/plans` | Catalog with prices and entitlements |
| `GET /v1/billing/subscription` | Current status, plan, trial end, usage |
| `POST /v1/billing/checkout` | Create a Razorpay subscription; returns its id + public key |
| `POST /v1/billing/cancel` | Cancel at cycle end |
| `POST /v1/webhooks/razorpay` | Webhook receiver |

Razorpay has **no equivalent of Stripe's hosted Billing Portal**, so the "send
them to the provider's UI" option this design originally took does not exist.
v1 therefore ships cancellation only — the one action users must be able to take
themselves — and routes card updates and plan changes through support. Building
a full self-service portal is deferred rather than assumed.

Cancellation uses Razorpay's cancel-at-cycle-end so a user who cancels keeps
what they paid for until the period ends.

## Frontend changes (`inboxos-web`)

- `TrialPill.tsx` — read real days remaining; today it is a hardcoded string
- `SubscribeBanner.tsx` — show on `past_due` and locked, link to checkout
- `plans.ts` — trial copy 14 days → 7 days
- New plan-picker step in onboarding, after connect
- Locked-state handling: a `402` from any endpoint routes to the plan picker

## Migration

One Alembic revision on the current head (`e7b2140c9a83`): create both tables,
add `Meeting.duration_seconds`, and backfill a `subscriptions` row for every
existing user with `status = trialing`, `plan_id = pro`, `interval = monthly`,
and `trial_ends_at = now() + 7 days`. They trial on Pro so that shipping billing
never removes a capability someone had the day before.

Existing users have no Razorpay customer, so their trial is local until they
complete checkout. This is the reason `trial_ends_at` lives on our row rather
than being read from the provider on demand.

## Testing

- **Entitlement matrix** — table-driven across plan × feature × access state,
  including comped and past-due
- **Webhook handlers** — recorded Razorpay fixtures; assert signature
  verification runs on the raw body, and that replay of the same
  event ID produces one effect, and that an out-of-order `subscription.updated`
  does not roll `current_period_end` backwards
- **Quota boundaries** — bot-hours at 4.9h, exactly 5.0h, and 5.1h; drafts at
  19, 20, 21
- **Worker gating** — a locked user's calendar sweep books no bot and calls no
  LLM; assert on the absence of the outbound call, not just on the return value
- **Retention** — a recording past its window loses media and transcript but
  keeps summary, decisions, and action items
- **Migration** — an existing user gets a trial row; running the migration twice
  does not shorten an already-granted trial

## Configuration

`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`,
`RAZORPAY_PLAN_STARTER_MONTHLY`, `RAZORPAY_PLAN_STARTER_ANNUAL`,
`RAZORPAY_PLAN_PRO_MONTHLY`, `RAZORPAY_PLAN_PRO_ANNUAL`, added to
`src/core/config.py` following the existing `Settings` pattern.

`RAZORPAY_KEY_ID` is public — it is handed to the browser to open the checkout
modal. `RAZORPAY_KEY_SECRET` and `RAZORPAY_WEBHOOK_SECRET` never leave the server.

## Known gaps

1. **Trial length conflict.** The site says 14 days in two files. Must ship in
   the same release as this change.
2. **"Save 25%" is approximate.** $19→$15 is 21% off; $39→$29 is 26%. Either
   soften the label or adjust a price.
3. **Team and Enterprise remain purchasable-looking on the site** while
   unimplemented. Their CTAs should route to a contact form, not Checkout, until
   the workspace subsystem exists.
4. **Advertised overage rates are not billed.** The site quotes $0.90 and $0.80
   per bot-hour. This spec hard-caps instead. Until overage ships, the pricing
   page should not promise it.
5. **USD-only may not serve domestic Indian customers.** Indian acquirers
   generally require domestic card transactions to settle in INR, so a USD-priced
   plan may be declined for an Indian cardholder paying with an Indian card —
   which would defeat half the reason for choosing Razorpay. This must be
   confirmed with Razorpay before launch. The `currency` column and a
   currency-keyed plan lookup exist so adding an INR plan set is additive
   configuration rather than a schema change.
6. **International card acceptance is approval-gated.** Razorpay auto-enables it
   for some businesses and requires an application from others, contingent on
   completed KYC and a website with defined policy pages. US acceptance is not
   available by default on a new account.
7. **Recurring mandates carry RBI conditions in India:** OTP authentication at
   mandate registration, and a pre-debit notification 24 hours before each
   charge. The ₹15,000 mandate registration cap is comfortably above the
   $19–$39 price points, so it does not bind here.
8. **No card-update path in v1.** A customer whose card expires can cancel and
   re-subscribe, or contact support. If churn from expiring cards becomes
   visible in the data, self-service card update is the first thing to build.
