# Invite-only signups — restore the paywall, close the front door

**Date:** 2026-08-24
**Status:** Approved, ready for implementation planning
**Repos:** `InboxPilot` (API) and `inboxos-web` (web)

## Goal

Turn payments back on, and stop taking public signups while we onboard the
first ~100 customers by hand. Existing users keep working. New users get in
only by invitation, and when they do they get a 14-day trial and then pay like
anyone else.

Two independent changes ship together because either one alone is wrong:
restoring the paywall without closing signups means strangers hit checkout
before we are ready to support them; closing signups without restoring the
paywall means the 100 people we hand-pick get the product for free forever.

## Scope

**In:** an `invited_emails` allowlist checked in the Google OAuth callback; a
CLI to manage it; restoring the seven `BILLING DISABLED` sites across both
repos; `TRIAL_DAYS` 7 → 14 and the six web copy sites that hard-code "7 days";
a migration backfilling everyone who signed up while billing was off; rewiring
the marketing acquisition CTAs so they no longer dead-end.

**Out, deliberately:**

- **Revoking an existing user's access.** `User.is_active` exists
  (`src/models/users.py:26`) and nothing reads it. The gate designed here asks
  one question — "is this a signup?" — and existing users pass unconditionally.
  Making `is_active` load-bearing is a separate change touching every auth
  dependency, not a side effect of this one.
- **A self-serve waitlist form.** "Book a call" already exists in the marketing
  navbar (`Navbar.tsx:41`) and is a better filter than a form for the first 100.
- **An admin web UI for invites.** A CLI script serves one operator managing
  ~100 rows. A UI needs auth, roles, and an admin surface none of which exist.
- **Enforcing 100 as a number in code.** The list *is* the cap — insert 100
  rows, get 100 customers. A `SIGNUP_CAP` setting would be a second number to
  keep in sync with the first, for no benefit.
- **Invite codes.** Considered and rejected: codes need a transient cookie
  through the Google round-trip, single-use race handling, and a code-entry UI.
  We know our first 100 customers' email addresses — we are booking calls with
  them. Codes solve the problem of *not* knowing, which we do not have.

## Decisions

| Decision | Choice |
|---|---|
| What an invite grants | Signup only. Invited users hit the real paywall: 14-day trial, plan picker, Razorpay mandate |
| Trial length | 14 days for everyone (`TRIAL_DAYS` 7 → 14), not per-invite |
| Gate identity | Google's **verified** email from the ID token |
| Gate storage | `invited_emails` table in Postgres |
| Gate position | `google_callback`, on the signup branch only |
| Admin surface | `scripts/invite.py add \| list \| revoke` |
| Refusal signal | `SignupNotInvited` exception → redirect `/login?error=not_invited` |
| Existing users | Always allowed in, no invite check |
| Billing-off-window accounts | Backfilled a fresh 14-day trial by migration |
| Acquisition funnel | Marketing CTAs → "Book a call"; `/login` stays for invitees |
| 100 cap | Not enforced in code |

### Why not a per-invite trial length

While signups are closed, **every new user is an invited user.** There is no
second population to distinguish, so a `trial_days` column on `invited_emails`
would have exactly one value in every row. Setting `TRIAL_DAYS = 14` and
leaving the existing trial machinery untouched gets the same result with no new
billing logic. If we later reopen public signups *and* want invitees treated
differently, that is the moment to add the column — not now.

## Architecture

### Data model

New table `invited_emails`:

| Column | Notes |
|---|---|
| `id` | UUID PK, `UUIDMixin` |
| `email` | `String(320)`, unique, indexed, stored **lowercased** |
| `note` | `String(255)`, nullable — who they are, which call they came from |
| `invited_at` | timestamptz, not null, default now |
| `claimed_at` | timestamptz, nullable — null until the invite is used |
| `claimed_by_user_id` | FK `users.id`, `ondelete SET NULL`, nullable |

`created_at`/`updated_at` come from `TimestampMixin`, matching every other
model. `invited_at` is kept separate from `created_at` on purpose: a row may be
re-inserted or corrected, and "when did we decide to invite this person" is a
product fact we will want in a funnel query, not an audit timestamp.

`claimed_by_user_id` is `SET NULL` rather than `CASCADE`: if a user is deleted
we want the invite record to survive, showing the spot was used. `CASCADE`
would silently free up a slot and lose the history.

**Lowercasing is the gate's correctness hinge.** Google returns the address as
the user's provider spells it; `nilesh@x.com` and `Nilesh@X.com` are the same
mailbox. Both the CLI (on insert) and `is_invited` (on lookup) normalise with
`.strip().lower()`. The unique index is on the stored lowercase value, so the
database refuses a duplicate that differs only in case.

### The gate

New `src/services/auth/invites.py`:

```python
async def is_invited(db: AsyncSession, email: str) -> bool
async def claim(db: AsyncSession, email: str, user_id: uuid.UUID) -> None
```

`claim` is idempotent — it sets `claimed_at`/`claimed_by_user_id` only when
`claimed_at` is null, so a re-run cannot rewrite who claimed a spot.

New `SignupNotInvited` in `src/core/exceptions.py`, carrying the email for logs.

`upsert_user_from_google` (`src/services/auth/oauth.py:13`) gains one keyword
argument and one return value:

```python
async def upsert_user_from_google(
    db: AsyncSession, profile: dict, *, signup_allowed: bool
) -> tuple[User, bool]:          # (user, created)
    user = await db.scalar(select(User).where(User.google_sub == profile["sub"]))
    now = datetime.now(timezone.utc)

    if user is None:                                  # ← signup
        if not signup_allowed:
            raise SignupNotInvited(profile["email"])
        ...
```

The refusal is an **exception, not a `False` return**, so no caller can add a
second login path and forget to check. It is raised before `db.add`, so a
refused signup commits nothing.

`google_callback` (`src/api/v1/auth.py:70`) becomes:

```python
signup_allowed = await invites.is_invited(db, profile["email"])
try:
    user, created = await oauth.upsert_user_from_google(
        db, profile, signup_allowed=signup_allowed
    )
except SignupNotInvited:
    resp = RedirectResponse(f"{settings.LOGIN_URL}?error=not_invited", 303)
    resp.delete_cookie(STATE_COOKIE, path="/")
    resp.delete_cookie(VERIFIER_COOKIE, path="/")
    return resp

if created:
    await invites.claim(db, profile["email"], user.id)
```

The refusal path must clear the PKCE cookies exactly like the success path —
otherwise a rejected visitor carries a stale 10-minute `oauth_state` into their
next attempt.

Costs one extra `SELECT` per login. `is_invited` is evaluated for existing users
too, and its result ignored on that branch; keeping the call unconditional means
the callback reads as one linear flow rather than branching on user existence
twice.

**The gate matches on the ID-token email, which Google has verified** — it is
not user-supplied and cannot be spoofed by editing a form. `google_sub`, not
email, remains the identity key for *existing* users, so a customer who changes
their Google display email still logs in.

### Admin CLI

`scripts/invite.py`, following the conventions of `scripts/google_connect_url.py`:

- `add <email> [--note TEXT]` — upsert, normalised; reports whether the row was
  new or already present
- `list [--claimed | --unclaimed]` — email, note, invited_at, claimed_at; prints
  the claimed/total count so "how many of the 100 are gone" is one command
- `revoke <email>` — deletes an **unclaimed** row. Refuses to revoke a claimed
  one, because deleting that row does not remove the user's access (existing
  users always pass the gate) and would only destroy the record of how they got
  in. Revoking real access is the `is_active` work listed as out of scope.

## Restoring the paywall

Seven changes across eight files, plus a migration. The five source changes
each carry a `BILLING DISABLED` comment explaining what to restore; the test
changes are found by their skip reasons, not by that marker, so a
`grep "BILLING DISABLED"` alone does not surface rows 6 and 7.

| # | File | Change |
|---|---|---|
| 1 | `src/services/billing/access.py` | delete 2 early returns, uncomment `resolve_access` + `effective_plan_id` bodies |
| 2 | `src/api/v1/billing.py:150` | delete `return True`, uncomment `_subscription_started` body |
| 3 | `src/api/v1/webhooks.py:173` | uncomment the signature check, drop the `noqa: F841` |
| 4 | `inboxos-web` `src/app/dashboard/layout.tsx:28` | uncomment the gate and the two imports at line 6 |
| 5 | `inboxos-web` `src/app/onboarding/notetaker/page.tsx:79` | restore `router.replace("/onboarding/plan")`, drop the `/dashboard` line |
| 6 | `tests/test_mail_access.py:27`, `tests/test_mail_sync_trigger.py:66` | remove both `pytest.mark.skip` |
| 7 | `tests/test_billing_disabled_gate.py` | **delete outright** — its assertions deliberately contradict the real spec |

Restoring #1 and #2 without #4 leaves the web app's own gate off; restoring #4
without #1 locks users out of a dashboard the API still serves. They ship as one
commit.

The `noqa: F401` comments guarding imports kept alive for the commented-out
bodies (`access.py:23-30`, `billing.py:29-32`) come off with the code they
guard.

### Migration: the billing-off-window accounts

Accounts created since 2026-08-18 have **no `subscriptions` row**.
`get_or_create_subscription` runs only from `start_checkout`
(`src/api/v1/billing.py:243`); a plain `GET /billing/subscription` never creates
one. So the moment `resolve_access` stops returning `entitled`, every one of
them is both locked *and* bounced to `/onboarding/plan`.

New migration, mirroring `f1a2b3c4d5e6`'s backfill:

```sql
INSERT INTO subscriptions
    (id, user_id, plan_id, interval, currency, status, trial_ends_at,
     trial_consumed, cancel_at_period_end, comped, created_at, updated_at)
SELECT gen_random_uuid(), u.id, 'pro', 'monthly', 'USD', 'authenticated',
       now() + interval '14 days', true, false, false, now(), now()
FROM users u
ON CONFLICT (user_id) DO NOTHING;
```

`status = 'authenticated'` is Razorpay's "mandate signed, first charge not yet
due" — the state our access rules read as *trialing* — even though no Razorpay
subscription exists. `trial_consumed = true` because this row's creation **is**
the trial grant; without it the next checkout hands these users a second
full-length trial, which is the exact bug `trial_consumed` was added to close
(`d4e5f6a7b8c9`). `ON CONFLICT DO NOTHING` makes a re-run harmless and, more
importantly, means anyone who *did* reach checkout keeps the trial already
counting down rather than having it restarted.

The migration also backfills these users into `invited_emails` as
already-claimed, so they count against the 100 and `list` reflects reality.

**The `14` is a hard-coded literal, not `settings.TRIAL_DAYS`.**

### Bug found while reading: a migration that reads live config

`f1a2b3c4d5e6:99` interpolates the *current* setting into its SQL at runtime:

```python
""".replace(":days", str(TRIAL_DAYS))
```

Migrations are historical records and must be immutable, but this one's
behaviour changes whenever `TRIAL_DAYS` changes. Bumping the setting to 14
silently rewrites what that 2026-08-02 migration does on any database built
from scratch after today.

Harmless on the existing database — it has already run. Wrong in principle, and
a trap for anyone who rebuilds from zero. **Fix: freeze the literal to `7` in
that migration and drop the `TRIAL_DAYS` import.** One line, and it removes the
class of bug rather than this instance of it.

## Trial length: 7 → 14

`src/core/config.py:174`, `TRIAL_DAYS: int = 7` → `14`.

This resolves a discrepancy the billing spec already flagged. That spec's
"Decisions" section notes the marketing site advertised a 14-day Pro trial while
billing shipped 7, and required the site be corrected downward "or the site
oversells the trial". The site was duly changed to 7. Going to 14 now means
**changing all six of those copy sites back** — and the API is the source of
truth, so the copy follows it.

| File | Current text |
|---|---|
| `src/lib/plans.ts:44` | `cta: "Start 7-day Pro trial"` |
| `src/components/marketing/Hero.tsx:24` | `Start 7-day Pro trial` |
| `src/components/marketing/Pricing.tsx:151` | "The Pro trial runs 7 days with Pro's full 15 bot-hours included" |
| `src/components/marketing/Purpose.tsx:22` | "…with a seven-day trial" — spelled out, so a `grep "7"` misses it |
| `src/app/(marketing)/terms/page.tsx:66` | "The Pro trial runs 7 days and includes…" |
| `src/app/(marketing)/terms/page.tsx:68` | "…trial ends when the 7 days are up, not before" |

Two of these are the **Terms of Service** — a legal document stating the trial
length. It must not disagree with what the API grants.

`TrialPill.tsx` needs no change: it derives days remaining from
`sub.trial_ends_at` rather than hard-coding a length. That is the pattern the
other six should have followed, and worth noting for whoever next changes this
number — the durable fix is to serve the trial length from the API and render
it, so there is one writer instead of seven. Out of scope here; the copy edits
are the cheap correct move today, and #4 below is why Hero's line disappears
anyway.

## Frontend changes (`inboxos-web`)

### The acquisition funnel currently dead-ends

Every marketing CTA points at `/login`: `Navbar.tsx:45` **and** `:48` ("Log in"
and "Get started" go to the same href), `Hero.tsx:23`, and `Pricing.tsx:26`
(`return "/login"`). With signups closed, "Get started" means *complete the
entire Google OAuth flow, grant mailbox scopes, then get rejected* — the worst
possible first impression, and it burns a Google consent prompt to deliver it.

The fix uses what the navbar already has — **"Book a call"**
(`Navbar.tsx:41`) — making the funnel:

> Book a call → operator runs `scripts/invite.py add` → they log in, 14-day trial

Changes:

1. `Navbar.tsx:48` "Get started" → the Book-a-call href
2. `Hero.tsx:23-24` → Book-a-call, copy from "Start 7-day Pro trial" to a
   request-access line (this supersedes its row in the trial-copy table above)
3. `Pricing.tsx:26` → return the Book-a-call href. Read its comment at :18
   first — it documents the deliberate decision to funnel pricing through
   `/login`, and that comment needs updating rather than contradicting
4. `Navbar.tsx:45` "Log in" → **unchanged**, still `/login`

### `/login` explains the refusal

`src/app/login/page.tsx` reads `?error=not_invited` from `useSearchParams` and
renders one line above the Google button:

> InboxPilot is invite-only while we onboard our first users — book a call and
> we'll get you set up.

…with the Book-a-call link. No new page, no waitlist form. The page is already
`"use client"`, so this is local state plus a hook.

Note the page's existing `backendConfigured()` / `mockSignIn` fallback: with no
backend configured it signs in locally and never touches the invite gate. That
is correct — it is the no-backend dev path — but it means **the gate cannot be
tested through the web UI without a real backend.** API tests are what prove
the gate; the web change is presentation only.

### `POST_LOGIN_REDIRECT_URL` has no sibling for failures

`settings.POST_LOGIN_REDIRECT_URL` (`src/core/config.py:35`) is the only web URL
the API knows. The refusal redirect needs a login URL, so add
`LOGIN_URL: str = "http://localhost:3000/login"` alongside it, set per
environment the same way. Deriving it from `POST_LOGIN_REDIRECT_URL` by string
surgery would break the moment that value changes.

## Google consent screen

Worth doing regardless, and it interacts with this work. While the OAuth app is
unverified, Google caps it at 100 test users and only listed addresses can
complete the flow — a cap that happens to match our target exactly. Adding each
invited address as a test user means an uninvited person is stopped by Google
*before* reaching our callback, with `invited_emails` as the second line of
defence rather than the only one.

Two lists to keep in sync; `scripts/invite.py list` output is what gets pasted.
And the app-level gate is what survives verification: the day the app is
published, Google's cap evaporates and the table is all that remains. That is
the reason not to rely on the consent screen alone.

## Testing

TDD — tests before implementation.

**New, `tests/test_signup_invite.py`:**

| Case | Expect |
|---|---|
| Existing user (`google_sub` matches), no invite row | logs in fine — the gate never applies |
| New user, invite exists and unclaimed | user created, invite `claimed_at`/`claimed_by_user_id` set |
| New user, no invite | `SignupNotInvited`; redirect to `/login?error=not_invited`; **`users` row count unchanged** |
| New user, invite differs only in case (`Nilesh@X.com`) | allowed — normalisation works both ways |
| Invite already claimed, different `google_sub` presents same email | allowed in (email is the invited unit, and the claim is a record not a lock) — asserted so the semantics are deliberate rather than accidental |
| Refused signup | PKCE `oauth_state`/`oauth_verifier` cookies cleared |
| `claim` run twice | `claimed_by_user_id` unchanged by the second call |

**Migration:** applying it grants exactly one subscription per pre-existing
user; a user who already has a row keeps their original `trial_ends_at`;
re-running changes nothing.

**Restored, and the real proof the paywall is back:** `tests/test_mail_access.py`
and `test_does_not_start_before_checkout` in `tests/test_mail_sync_trigger.py`
must pass once un-skipped. If they do not, the restore is incomplete.

`tests/test_billing_disabled_gate.py` is deleted, so its absence from the run is
expected, not a regression.

## Configuration

| Setting | Value |
|---|---|
| `TRIAL_DAYS` | `7` → `14` |
| `LOGIN_URL` | new; `http://localhost:3000/login` in local, the real host elsewhere |

`LOGIN_URL` must be set in every deployed environment before this ships, or a
refused signup redirects to localhost. It needs adding wherever
`POST_LOGIN_REDIRECT_URL` is already set (`render.yaml`, `infra/`,
`scripts/push-secrets.sh`).

## Known gaps

- **Nothing reads `User.is_active`.** There is no way to revoke a user who is
  already in. Accepted for the first 100 — they are hand-picked and we can talk
  to them.
- **Trial length is written in seven places.** Reduced from a bug to a
  maintenance cost by this spec; the durable fix (serve it from the API) is
  deferred.
- **Two access lists.** Google's test users and `invited_emails` are maintained
  by hand from the same source. Diverging means a confusing "Google says no,
  our table says yes".
- **`claimed_at` is a record, not a lock.** Two Google accounts sharing an email
  address (a rename) could both pass. Adding a second user for one invite is
  possible; it is also visible in `list`, and unlikely enough at n=100 to leave.
