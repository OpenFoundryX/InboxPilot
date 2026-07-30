# Onboarding Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mock onboarding pages with three real steps — scheduled mail, categorization, meeting bot — that run after the Gmail/Calendar connect step and write to the settings endpoints that already exist.

**Architecture:** A durable `onboarded_at` timestamp on `users` (backend) replaces the frontend's "both integrations connected means onboarded" assumption, so the wizard can span four routes without becoming skippable. Each step owns one question, PUTs its own settings on Continue, and defers the two side-effecting calls (`mailman/start`, `onboarding/complete`) to the final step.

**Tech Stack:** FastAPI + async SQLAlchemy + Alembic (`InboxPilot`); Next.js 14 App Router + React 18 + Tailwind (`inboxos-web`). Both are separate git repos under `/Users/abcom/Desktop/openfoundry/`.

**Spec:** `docs/superpowers/specs/2026-07-28-onboarding-simplification-design.md`

## Global Constraints

- **Two repos.** Task 1 lands in `/Users/abcom/Desktop/openfoundry/InboxPilot`. Tasks 2–6 land in `/Users/abcom/Desktop/openfoundry/inboxos-web`. Each has its own git history — commit in the repo you are editing.
- **No new tests, no test infrastructure.** The user chose static checks plus a manual walk. Do not add pytest fixtures, Vitest, or any test runner. Verify with `uv run ruff check src` + `uv run mypy src` (backend) and `npx tsc --noEmit` + `npm run lint` (frontend).
- **Ruff line-length is 100** (`pyproject.toml`). Keep backend lines under it.
- **Builtin category keys are fixed** and MUST be spelled exactly: `to_do`, `to_follow_up`, `notification`, `fyi`, `marketing`, `noise`. They name Gmail labels that already exist in users' mailboxes (`src/models/categorization.py`).
- **`auto_join` is never preselected.** `src/models/meetings.py:83` records that recording other people is a deliberate user choice. The notetaker step defaults to `auto_join: false`.
- **The frontend must survive `backendConfigured() === false`.** Every step falls back to the `DEFAULT_SETTINGS` its `lib/` module already exports when the initial GET fails, and skips writes entirely when the backend is not configured.
- **Frontend API calls go through `apiFetch`** from `@/lib/api`, which prefixes `/api` and throws `ApiError` on non-2xx. Never call `fetch` directly.
- **Task order matters.** Task 6 flips the routing. Doing it earlier creates a redirect loop: the dashboard would bounce to `/onboarding/connect`, whose Continue button goes to the dashboard.

## File Structure

**`InboxPilot`:**

| File | Responsibility |
| --- | --- |
| `src/models/users.py` (modify) | Add the `onboarded_at` column |
| `alembic/versions/<hash>_add_onboarded_at_to_users.py` (create) | Add column, backfill existing rows |
| `src/schemas/user.py` (modify) | Expose `onboarded_at` on `UserRead` |
| `src/api/v1/users.py` (modify) | `POST /users/me/onboarding/complete` |

**`inboxos-web`:**

| File | Responsibility |
| --- | --- |
| `src/lib/onboarding.ts` (create) | `completeOnboarding()` + the shared `finishOnboarding()` helper |
| `src/lib/session.ts` (modify) | `Access` gains `connected`; `onboarded` reads `onboarded_at` |
| `src/lib/auth.ts` (modify) | Drop the dead `inboxos_inbox_pref` key |
| `src/components/onboarding/OnboardingStepper.tsx` (modify) | The four real steps |
| `src/components/onboarding/StepShell.tsx` (create) | Shared heading + Continue/Skip footer + error card |
| `src/app/onboarding/mail/page.tsx` (create) | Step 2 |
| `src/app/onboarding/categories/page.tsx` (create) | Step 3 |
| `src/app/onboarding/notetaker/page.tsx` (create) | Step 4 + finish |
| `src/app/onboarding/layout.tsx` (modify) | Gate on `connected` / `onboarded` |
| `src/app/onboarding/connect/page.tsx` (modify) | Continue goes to step 2 |
| `src/app/dashboard/layout.tsx` (modify) | Redirect target for un-onboarded users |
| `src/app/dashboard/page.tsx` (modify) | Toast when batching failed to activate |
| `src/app/dashboard/settings/page.tsx` (modify) | "Replay onboarding" targets the new first step, mock-only |
| `src/app/login/page.tsx` (modify) | Mock sign-in targets the new first step |
| `src/app/onboarding/{calendar,inbox,notes,creating}/page.tsx` (delete) | Mock flow |

---

### Task 1: Backend — `onboarded_at` and the completion endpoint

**Repo:** `/Users/abcom/Desktop/openfoundry/InboxPilot`

**Files:**
- Modify: `src/models/users.py`
- Create: `alembic/versions/<generated>_add_onboarded_at_to_users.py`
- Modify: `src/schemas/user.py`
- Modify: `src/api/v1/users.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `POST /v1/users/me/onboarding/complete` → `UserRead` (200). `GET /v1/auth/me` → `UserRead` now including `onboarded_at: str | null` (ISO 8601). Task 2 consumes both.

- [ ] **Step 1: Add the column to the model**

In `src/models/users.py`, add below the `last_login_at` line:

```python
    # Set once, when the user finishes the onboarding wizard. NULL means the
    # wizard is unfinished — the frontend uses it to gate the dashboard.
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

`datetime`, `DateTime`, `Mapped`, and `mapped_column` are already imported at the top of the file.

- [ ] **Step 2: Generate the migration**

The current head is `bb9e0a302824`. Run:

```bash
make migrate   # ensure the DB is at head first
uv run alembic revision -m "add onboarded_at to users"
```

- [ ] **Step 3: Fill in the migration body**

In the generated file, set `down_revision` and write the two functions. Keep the
`revision` value Alembic generated — do not invent one.

```python
down_revision: Union[str, None] = 'bb9e0a302824'


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing users already connected both accounts under the old flow. Treat
    # them as onboarded so nobody in the middle of using the product is pulled
    # into the new wizard; they configure these features from the dashboard.
    op.execute("UPDATE users SET onboarded_at = now()")


def downgrade() -> None:
    op.drop_column("users", "onboarded_at")
```

- [ ] **Step 4: Apply it and confirm the round trip**

```bash
make migrate
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: all three succeed. After the final upgrade, existing rows have a
non-null `onboarded_at`.

- [ ] **Step 5: Expose it on `UserRead`**

In `src/schemas/user.py`, add as the last field of `UserRead`:

```python
    onboarded_at: datetime | None = None
```

- [ ] **Step 6: Write the endpoint**

Replace the whole of `src/api/v1/users.py` with:

```python
"""User management routes (API v1)."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import DbSession
from models.users import User
from schemas.user import UserRead
from services.auth.dependencies import get_current_user

router = APIRouter(prefix="/users", tags=["users"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("/me/onboarding/complete", response_model=UserRead)
async def complete_onboarding(user: CurrentUser, db: DbSession) -> User:
    """Mark the onboarding wizard finished.

    Idempotent: a repeat call (double-submit, or a retry after a network blip)
    leaves the original timestamp alone rather than moving it.
    """
    if user.onboarded_at is None:
        user.onboarded_at = datetime.now(timezone.utc)
    return user
```

`db` is unused in the body on purpose — depending on `DbSession` is what puts the
request inside the session whose teardown commits the change, the same way
`api/v1/mailman.py:update_settings` mutates and returns without an explicit
commit.

- [ ] **Step 7: Register nothing, verify it is already wired**

`src/api/router.py:10` already does `api_router.include_router(users.router)`. No
change needed. Confirm the route appears:

```bash
uv run ruff check src && uv run mypy src
```

Expected: both clean.

- [ ] **Step 8: Verify by hand**

Start the stack (`make up`), sign in through the browser so you hold a session
cookie, then in the browser devtools console on the frontend origin:

```js
await (await fetch("/api/users/me/onboarding/complete", { method: "POST" })).json()
```

Expected: JSON with a non-null `onboarded_at`. Run it a second time and confirm
the timestamp is unchanged. Confirm `GET /api/auth/me` returns the same value.

- [ ] **Step 9: Commit**

```bash
git add src/models/users.py src/schemas/user.py src/api/v1/users.py alembic/versions
git commit -m "feat: track onboarding completion with users.onboarded_at"
```

---

### Task 2: Web — session gating primitives and the stepper

**Repo:** `/Users/abcom/Desktop/openfoundry/inboxos-web`

**Files:**
- Create: `src/lib/onboarding.ts`
- Modify: `src/lib/session.ts`
- Modify: `src/lib/auth.ts`
- Modify: `src/components/onboarding/OnboardingStepper.tsx`
- Create: `src/components/onboarding/StepShell.tsx`

**Interfaces:**
- Consumes: `POST /v1/users/me/onboarding/complete` and `onboarded_at` on `/v1/auth/me` from Task 1.
- Produces:
  - `type Access = { authed: boolean; connected: boolean; onboarded: boolean }` and `checkAccess(): Promise<Access>` from `@/lib/session`
  - `completeOnboarding(): Promise<UserRead>` and `finishOnboarding(activateBatching: boolean): Promise<{ batchingFailed: boolean }>` from `@/lib/onboarding`
  - `StepShell` from `@/components/onboarding/StepShell`, props: `{ title: string; blurb: string; error: string | null; busy: boolean; continueLabel?: string; onContinue: () => void; onSkip: () => void; children: ReactNode }`
  - `BATCHING_FAILED_KEY = "inboxos_batching_failed"` exported from `@/lib/onboarding`

This task changes no routing. After it, the app behaves exactly as before.

- [ ] **Step 1: Add `onboarded_at` to the `UserRead` type**

In `src/lib/session.ts`, add to the `UserRead` type:

```ts
  onboarded_at?: string | null;
```

- [ ] **Step 2: Create `src/lib/onboarding.ts`**

```ts
import { apiFetch } from "./api";
import { setOnboarded } from "./auth";
import { startBatching } from "./mailman";
import { backendConfigured, type UserRead } from "./session";

/** Set by the last onboarding step when batching could not be activated, read
 *  once by the dashboard. sessionStorage, not localStorage: the notice is about
 *  this hand-off, not a durable piece of user state. */
export const BATCHING_FAILED_KEY = "inboxos_batching_failed";

/** Written by the scheduled-mail step, read by the last step, which owns the
 *  Finish click. "live" means never call startBatching(). It lives here rather
 *  than in the step's own module so the two steps do not import each other. */
export const BATCHING_CHOICE_KEY = "inboxos_batching_choice";

export const completeOnboarding = () =>
  apiFetch<UserRead>("/users/me/onboarding/complete", { method: "POST" });

/** The last step's write. Activating batching installs a Gmail filter, so it is
 *  deliberately deferred to here rather than run when the user picks a schedule.
 *
 *  A failed activation is not fatal — the user still lands on the dashboard,
 *  which reports it. Trapping someone in a wizard over a Gmail API hiccup is
 *  worse than an unbatched inbox they can fix from /dashboard/mailman. A failed
 *  completeOnboarding IS fatal and throws, because without it they re-enter the
 *  wizard on next login. */
export async function finishOnboarding(
  activateBatching: boolean,
): Promise<{ batchingFailed: boolean }> {
  if (!backendConfigured()) {
    setOnboarded();
    return { batchingFailed: false };
  }

  let batchingFailed = false;
  if (activateBatching) {
    try {
      await startBatching();
    } catch {
      batchingFailed = true;
      window.sessionStorage.setItem(BATCHING_FAILED_KEY, "1");
    }
  }

  await completeOnboarding();
  return { batchingFailed };
}
```

- [ ] **Step 3: Widen `Access` in `src/lib/session.ts`**

Replace the `Access` type and `checkAccess` function with:

```ts
export type Access = { authed: boolean; connected: boolean; onboarded: boolean };

/** `connected` is both integrations granted; `onboarded` is the wizard actually
 *  finished. They used to be the same flag, which let a user reach the dashboard
 *  without ever seeing the settings steps. Without a backend, the mock flags
 *  stand in — there is no onboarded_at to read. */
export async function checkAccess(): Promise<Access> {
  if (backendConfigured()) {
    const me = await getMe();
    if (!me) return { authed: false, connected: false, onboarded: false };
    const onboarded = Boolean(me.onboarded_at);
    try {
      const [gmail, calendar] = await Promise.all([
        getGmailStatus(),
        getCalendarStatus(),
      ]);
      return { authed: true, connected: gmail.connected && calendar.connected, onboarded };
    } catch {
      // Status unreachable → treat as not-yet-connected (send to connect step).
      return { authed: true, connected: false, onboarded };
    }
  }
  // `connected: true` without a backend — there is nothing to connect, and a
  // false here would ping-pong between the connect step and the dashboard.
  return { authed: isAuthed(), connected: true, onboarded: isOnboarded() };
}
```

- [ ] **Step 4: Trim `src/lib/auth.ts`**

Delete the `INBOX_PREF` constant and both lines that reference it — one in
`signOut`, one in `resetOnboarding`. The key belonged to the mock inbox page that
Task 6 deletes. The two functions become:

```ts
export function signOut(): void {
  if (typeof window === "undefined") return;
  clearFlag(KEY);
  clearFlag(ONBOARDED);
}
```

```ts
export function resetOnboarding(): void {
  if (typeof window === "undefined") return;
  clearFlag(ONBOARDED);
}
```

Keep `isOnboarded`, `setOnboarded`, and `resetOnboarding`. The no-backend path
has no `onboarded_at` to read, so without them the mock dashboard is unreachable
and `dashboard/settings/page.tsx` fails to compile.

Then confirm the dead key is gone:

```bash
grep -rn "inboxos_inbox_pref" src/lib/
```

Expected: no output. (`src/app/onboarding/inbox/page.tsx` still has one; Task 6
deletes that file.)

- [ ] **Step 5: Update the stepper to the four real steps**

In `src/components/onboarding/OnboardingStepper.tsx`, replace the `STEPS` array:

```tsx
const STEPS = [
  { href: "/onboarding/connect", label: "Connect accounts" },
  { href: "/onboarding/mail", label: "Scheduled mail" },
  { href: "/onboarding/categories", label: "Inbox labels" },
  { href: "/onboarding/notetaker", label: "Meeting notes" },
];
```

Leave the rest of the component alone.

- [ ] **Step 6: Create `src/components/onboarding/StepShell.tsx`**

```tsx
"use client";

import type { ReactNode } from "react";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";

type StepShellProps = {
  title: string;
  blurb: string;
  error: string | null;
  busy: boolean;
  continueLabel?: string;
  onContinue: () => void;
  onSkip: () => void;
  children: ReactNode;
};

/** Shared chrome for onboarding steps 2-4: heading on the left, the one question
 *  on the right, Continue + Skip underneath. Skip is safe on every step — all
 *  three features are off by default in the backend, so skipping leaves nothing
 *  half-configured. */
export default function StepShell({
  title,
  blurb,
  error,
  busy,
  continueLabel = "Continue",
  onContinue,
  onSkip,
  children,
}: StepShellProps) {
  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div className="pt-4">
        <h1 className="text-2xl font-extrabold tracking-tight">{title}</h1>
        <p className="mt-4 text-sm text-ink/60">{blurb}</p>
        <p className="mt-10 text-xs text-ink/40">You can change this anytime from your dashboard.</p>
      </div>
      <div>
        {children}
        {error ? (
          <Card className="mt-4 border border-accent/30 p-4 text-sm text-accent-dark">{error}</Card>
        ) : null}
        <Button variant="dark" onClick={onContinue} disabled={busy} className="mt-4 w-full">
          {busy ? "Saving…" : continueLabel}
        </Button>
        <button
          type="button"
          onClick={onSkip}
          disabled={busy}
          className="mt-3 w-full text-sm font-medium text-ink/50 hover:text-ink disabled:opacity-50"
        >
          Skip for now
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 7: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: both clean. `checkAccess` now returns a third field; the two existing
call sites destructure only what they use, so they still compile.

- [ ] **Step 8: Commit**

```bash
git add src/lib/onboarding.ts src/lib/session.ts src/lib/auth.ts src/components/onboarding
git commit -m "feat: onboarding completion flag and shared step chrome"
```

---

### Task 3: Web — step 2, scheduled mail

**Repo:** `/Users/abcom/Desktop/openfoundry/inboxos-web`

**Files:**
- Create: `src/app/onboarding/mail/page.tsx`

**Interfaces:**
- Consumes: `StepShell` and `BATCHING_CHOICE_KEY` from Task 2; `getSettings`, `updateSettings` from `@/lib/mailman`; `backendConfigured` from `@/lib/session`.
- Produces: the route `/onboarding/mail`. Writes `BATCHING_CHOICE_KEY` in `localStorage` as `"times" | "interval" | "live"`, which Task 5 reads to decide whether to call `startBatching()`.

- [ ] **Step 1: Write the page**

```tsx
"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import RadioGroup from "@/components/ui/RadioGroup";
import StepShell from "@/components/onboarding/StepShell";
import { backendConfigured } from "@/lib/session";
import { BATCHING_CHOICE_KEY } from "@/lib/onboarding";
import { getSettings, updateSettings, type SettingsUpdate } from "@/lib/mailman";

type Choice = "times" | "interval" | "live";

const OPTIONS = [
  {
    value: "times",
    label: "A few times a day",
    description: "Mail is held and delivered in three batches.",
  },
  {
    value: "interval",
    label: "Every 2 hours",
    description: "A steadier drip through your working day.",
  },
  {
    value: "live",
    label: "Keep mail arriving live",
    description: "No batching. Mail lands the moment it is sent.",
  },
];

/** The browser's zone beats the backend's UTC default — delivery slots are only
 *  meaningful in the user's own day. */
const browserTimezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

function payloadFor(choice: Choice): SettingsUpdate | null {
  if (choice === "times") {
    return { delivery_mode: "times", times_per_day: 3, timezone: browserTimezone() };
  }
  if (choice === "interval") {
    return { delivery_mode: "interval", interval_hours: 2, timezone: browserTimezone() };
  }
  return null;
}

export default function MailStep() {
  const router = useRouter();
  const [choice, setChoice] = useState<Choice>("times");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Pre-fill from whatever is already saved so a resumed wizard shows the real
  // state. A failed fetch is not worth an error here — the defaults are fine.
  useEffect(() => {
    if (!backendConfigured()) return;
    let active = true;
    getSettings()
      .then((s) => {
        if (!active) return;
        setChoice(s.delivery_mode === "interval" ? "interval" : "times");
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  const next = () => router.push("/onboarding/categories");

  async function onContinue() {
    setBusy(true);
    setError(null);
    try {
      const payload = payloadFor(choice);
      if (payload && backendConfigured()) {
        await updateSettings(payload);
      }
      window.localStorage.setItem(BATCHING_CHOICE_KEY, choice);
      next();
    } catch {
      setError("Couldn't save your delivery schedule. Try again.");
      setBusy(false);
    }
  }

  function onSkip() {
    window.localStorage.setItem(BATCHING_CHOICE_KEY, "live");
    next();
  }

  return (
    <StepShell
      title="When should we deliver your email?"
      blurb="InboxOS holds new mail and hands it to you in batches, so you read email when you choose to instead of the moment it arrives. Anyone on your VIP list always comes straight through."
      error={error}
      busy={busy}
      onContinue={onContinue}
      onSkip={onSkip}
    >
      <RadioGroup options={OPTIONS} value={choice} onChange={(v) => setChoice(v as Choice)} />
    </StepShell>
  );
}
```

- [ ] **Step 2: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: both clean.

- [ ] **Step 3: Check it renders**

With the dev server running (`npm run dev`) and signed in, open
`http://localhost:3000/onboarding/mail` directly. Expected: the stepper shows
"Scheduled mail" as step 2, three options render, "A few times a day" is
preselected. Do not click Continue yet — `/onboarding/categories` does not exist
until Task 4.

- [ ] **Step 4: Commit**

```bash
git add src/app/onboarding/mail/page.tsx
git commit -m "feat: scheduled mail onboarding step"
```

---

### Task 4: Web — step 3, categorization

**Repo:** `/Users/abcom/Desktop/openfoundry/inboxos-web`

**Files:**
- Create: `src/app/onboarding/categories/page.tsx`

**Interfaces:**
- Consumes: `StepShell` from Task 2; `getSettings`, `updateSettings`, `updateCategory` from `@/lib/categorization`.
- Produces: the route `/onboarding/categories`.

- [ ] **Step 1: Write the page**

The three choices and their copy come from the deleted mock `/onboarding/inbox`,
now wired to the API. `updateCategory(key, body)` issues one PATCH per category,
so a choice fans out to six calls; `Promise.all` is fine, the backend handles
them independently.

```tsx
"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import StepShell from "@/components/onboarding/StepShell";
import { backendConfigured } from "@/lib/session";
import {
  getSettings,
  updateCategory,
  updateSettings,
  type CategoryUpdate,
} from "@/lib/categorization";

type Choice = "attention" | "all" | "none";

/** The six builtin keys, fixed in the backend (models/categorization.py). They
 *  name Gmail labels that already exist in users' mailboxes. */
const ATTENTION_KEEP = ["to_do", "to_follow_up", "fyi", "notification"];
const ATTENTION_ARCHIVE = ["marketing", "noise"];
const ALL_KEYS = [...ATTENTION_KEEP, ...ATTENTION_ARCHIVE];

const OPTIONS: { value: Choice; title: string; subtitle: string; tags: string[] }[] = [
  {
    value: "attention",
    title: "Only what needs my attention",
    subtitle: "Marketing and noise get labelled and archived out of your inbox.",
    tags: ["To do", "To follow up", "FYI", "Notification"],
  },
  {
    value: "all",
    title: "All my emails",
    subtitle: "Everything gets a label, nothing leaves your inbox.",
    tags: ["To do", "To follow up", "FYI", "Notification", "Marketing", "Noise"],
  },
  {
    value: "none",
    title: "Don't label my emails",
    subtitle: "Keep your inbox exactly as it is.",
    tags: [],
  },
];

/** One PATCH body per builtin category for the given choice. `archive` is what
 *  makes "only what needs my attention" true rather than just a label. */
function categoryUpdates(choice: Choice): { key: string; body: CategoryUpdate }[] {
  if (choice === "attention") {
    return [
      ...ATTENTION_KEEP.map((key) => ({
        key,
        body: { is_enabled: true, actions: { archive: false } },
      })),
      ...ATTENTION_ARCHIVE.map((key) => ({
        key,
        body: { is_enabled: true, actions: { archive: true } },
      })),
    ];
  }
  if (choice === "all") {
    return ALL_KEYS.map((key) => ({
      key,
      body: { is_enabled: true, actions: { archive: false } },
    }));
  }
  return [];
}

export default function CategoriesStep() {
  const router = useRouter();
  const [choice, setChoice] = useState<Choice>("attention");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!backendConfigured()) return;
    let active = true;
    getSettings()
      .then((s) => {
        if (!active || s.is_enabled) return;
        setChoice("none");
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  const next = () => router.push("/onboarding/notetaker");

  async function onContinue() {
    setBusy(true);
    setError(null);
    try {
      if (backendConfigured()) {
        await updateSettings({ is_enabled: choice !== "none" });
        await Promise.all(
          categoryUpdates(choice).map(({ key, body }) => updateCategory(key, body)),
        );
      }
      next();
    } catch {
      setError("Couldn't save your label settings. Try again.");
      setBusy(false);
    }
  }

  return (
    <StepShell
      title="Choose what stays in your inbox"
      blurb="InboxOS reads each new email, labels it, and — if you want — moves the low-value ones out of the way. It never deletes anything."
      error={error}
      busy={busy}
      onContinue={onContinue}
      onSkip={next}
    >
      <div className="space-y-3">
        {OPTIONS.map((opt) => {
          const active = opt.value === choice;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => setChoice(opt.value)}
              className={`w-full rounded-2xl border p-4 text-left transition-colors ${
                active ? "border-ink bg-card" : "border-black/5 bg-card hover:border-ink/20"
              }`}
            >
              <div className="text-sm font-bold text-ink">{opt.title}</div>
              <div className="mt-0.5 text-xs text-ink/50">{opt.subtitle}</div>
              {opt.tags.length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {opt.tags.map((t) => (
                    <span key={t} className="rounded-full bg-cream px-2.5 py-1 text-xs text-ink/60">
                      {t}
                    </span>
                  ))}
                </div>
              ) : null}
            </button>
          );
        })}
      </div>
    </StepShell>
  );
}
```

- [ ] **Step 2: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: both clean. Note `CategoryUpdate.actions` is `Partial<CategoryActions>`,
so `{ archive: true }` alone type-checks.

- [ ] **Step 3: Check it renders and writes**

Open `http://localhost:3000/onboarding/categories`. Pick "All my emails" and
click Continue (it will 404 on `/onboarding/notetaker` — expected until Task 5).
Then check `http://localhost:3000/dashboard/categorization`: all six categories
should be enabled with no archive action.

- [ ] **Step 4: Commit**

```bash
git add src/app/onboarding/categories/page.tsx
git commit -m "feat: categorization onboarding step"
```

---

### Task 5: Web — step 4, meeting bot and finish

**Repo:** `/Users/abcom/Desktop/openfoundry/inboxos-web`

**Files:**
- Create: `src/app/onboarding/notetaker/page.tsx`

**Interfaces:**
- Consumes: `StepShell` from Task 2; `finishOnboarding` and `BATCHING_CHOICE_KEY` from `@/lib/onboarding`; `getNotetakerSettings`, `updateNotetakerSettings` from `@/lib/meetings`; `Orbit` from `@/components/onboarding/Orbit`.
- Produces: the route `/onboarding/notetaker`. This is the only step that calls `finishOnboarding`.

- [ ] **Step 1: Write the page**

```tsx
"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import RadioGroup from "@/components/ui/RadioGroup";
import StepShell from "@/components/onboarding/StepShell";
import Orbit from "@/components/onboarding/Orbit";
import { BATCHING_CHOICE_KEY, finishOnboarding } from "@/lib/onboarding";
import { backendConfigured } from "@/lib/session";
import { getNotetakerSettings, updateNotetakerSettings } from "@/lib/meetings";

type Choice = "ask" | "auto" | "off";

const OPTIONS = [
  {
    value: "ask",
    label: "Only when I ask",
    description: "Send the bot into a call from your dashboard, one meeting at a time.",
  },
  {
    value: "auto",
    label: "Join every meeting automatically",
    description: "The bot joins calendar meetings with two or more attendees.",
  },
  { value: "off", label: "No thanks", description: "No bot, no meeting notes." },
];

const SETTINGS: Record<Choice, { enabled: boolean; auto_join: boolean }> = {
  ask: { enabled: true, auto_join: false },
  auto: { enabled: true, auto_join: true },
  off: { enabled: false, auto_join: false },
};

export default function NotetakerStep() {
  const router = useRouter();
  // "ask" by default, never "auto": recording other people is the user's call to
  // make deliberately, not something onboarding switches on for them.
  const [choice, setChoice] = useState<Choice>("ask");
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!backendConfigured()) return;
    let active = true;
    getNotetakerSettings()
      .then((s) => {
        if (!active) return;
        setChoice(!s.enabled ? "off" : s.auto_join ? "auto" : "ask");
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  async function finish(save: boolean) {
    setBusy(true);
    setError(null);
    try {
      if (save && backendConfigured()) {
        await updateNotetakerSettings(SETTINGS[choice]);
      }
      setApplying(true);
      const activateBatching =
        window.localStorage.getItem(BATCHING_CHOICE_KEY) !== null &&
        window.localStorage.getItem(BATCHING_CHOICE_KEY) !== "live";
      await finishOnboarding(activateBatching);
      router.replace("/dashboard");
    } catch {
      // finishOnboarding only throws when completeOnboarding failed — the one
      // call that must land, or the user re-enters the wizard on next login.
      setApplying(false);
      setError("Couldn't finish setting up your account. Try again.");
      setBusy(false);
    }
  }

  if (applying) {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-8">
        <Orbit />
        <p className="text-lg font-semibold text-ink">Setting up your workspace…</p>
      </div>
    );
  }

  return (
    <StepShell
      title="Should we join your meetings?"
      blurb="A notetaker bot joins your calls, records them, and turns each one into a summary email with action items. Everyone on the call sees it join."
      error={error}
      busy={busy}
      continueLabel="Finish setup"
      onContinue={() => finish(true)}
      onSkip={() => finish(false)}
    >
      <RadioGroup options={OPTIONS} value={choice} onChange={(v) => setChoice(v as Choice)} />
    </StepShell>
  );
}
```

- [ ] **Step 2: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: both clean.

- [ ] **Step 3: Walk the three steps end to end**

Visit `/onboarding/mail`, pick "A few times a day", Continue → `/onboarding/categories`, pick "Only what needs my attention", Continue → `/onboarding/notetaker`, pick "Only when I ask", Finish.

Expected: the Orbit animation shows, then you land on `/dashboard`. Then verify:
- `GET /api/auth/me` → `onboarded_at` is set
- `GET /api/mailman/settings` → `is_active: true`, `delivery_mode: "times"`, `times_per_day: 3`
- `GET /api/meetings/settings` → `enabled: true`, `auto_join: false`
- `/dashboard/categorization` → `marketing` and `noise` have the archive action on

- [ ] **Step 4: Commit**

```bash
git add src/app/onboarding/notetaker/page.tsx
git commit -m "feat: meeting bot onboarding step and finish handler"
```

---

### Task 6: Web — wire the flow and delete the mock pages

**Repo:** `/Users/abcom/Desktop/openfoundry/inboxos-web`

**Files:**
- Modify: `src/app/onboarding/connect/page.tsx:106`
- Modify: `src/app/onboarding/layout.tsx`
- Modify: `src/app/dashboard/layout.tsx:22`
- Modify: `src/app/dashboard/page.tsx`
- Modify: `src/app/dashboard/settings/page.tsx:14-16`
- Modify: `src/app/login/page.tsx:16`
- Delete: `src/app/onboarding/calendar/`, `src/app/onboarding/inbox/`, `src/app/onboarding/notes/`, `src/app/onboarding/creating/`

**Interfaces:**
- Consumes: everything from Tasks 2–5.
- Produces: the finished flow. Nothing depends on this task.

This is the task that flips routing. Everything before it left the app's
behaviour unchanged; after it, the wizard is the real path.

- [ ] **Step 1: Point the connect step at step 2**

In `src/app/onboarding/connect/page.tsx`, change the Continue button (line ~106):

```tsx
        <Button
          variant="dark"
          disabled={!bothConnected}
          onClick={() => router.replace("/onboarding/mail")}
        >
          Continue
        </Button>
```

- [ ] **Step 2: Gate the onboarding layout**

In `src/app/onboarding/layout.tsx`, replace the `useEffect` and the full-bleed
branch. `connect` now shows the stepper like every other step, so only the
`pathname` check for it disappears:

```tsx
  useEffect(() => {
    let active = true;
    checkAccess().then(({ authed, connected, onboarded }) => {
      if (!active) return;
      if (!authed) {
        router.replace("/login");
        return;
      }
      // Already finished → the wizard is not somewhere to wander back into.
      if (onboarded) {
        router.replace("/dashboard");
        return;
      }
      // Nothing works without both grants, so the settings steps stay unreachable
      // until they land.
      if (!connected && !pathname.startsWith("/onboarding/connect")) {
        router.replace("/onboarding/connect");
        return;
      }
      setReady(true);
    });
    return () => {
      active = false;
    };
  }, [router, pathname]);
```

Then delete the `if (pathname.startsWith("/onboarding/creating") || ...)`
full-bleed block entirely, so every step renders inside the stepper layout.

- [ ] **Step 3: Fix the dashboard redirect target**

In `src/app/dashboard/layout.tsx`, line 22 currently sends mock users to the
deleted `/onboarding/creating`. Destructure `connected` too and replace the
redirect so a half-finished user resumes at the step they actually need rather
than re-reading the connect screen:

```tsx
    checkAccess().then(({ authed, connected, onboarded }) => {
      if (!active) return;
      if (!authed) {
        router.replace("/login");
        return;
      }
      if (!onboarded) {
        router.replace(connected ? "/onboarding/mail" : "/onboarding/connect");
        return;
      }
      setReady(true);
    });
```

Without a backend `checkAccess` reports `connected: true`, so the mock path lands
on the first settings step. The `backendConfigured` import in this file is now
unused — remove it from the import on line 5, leaving `import { checkAccess } from "@/lib/session";`.

- [ ] **Step 4: Report a failed batching activation on the dashboard**

In `src/app/dashboard/page.tsx`, add the import:

```tsx
import { BATCHING_FAILED_KEY } from "@/lib/onboarding";
```

Then, next to the other `useEffect` hooks in the page component, add:

```tsx
  // Set by the last onboarding step when startBatching() failed. Read once, so
  // it does not nag on every dashboard visit.
  useEffect(() => {
    if (window.sessionStorage.getItem(BATCHING_FAILED_KEY) !== "1") return;
    window.sessionStorage.removeItem(BATCHING_FAILED_KEY);
    setToast({
      id: Date.now(),
      text: "Couldn't turn on batched delivery. Turn it on from Mailman settings.",
      variant: "error",
    });
  }, []);
```

`setToast` and `<Toast>` already exist in this file (lines 63 and 157).

- [ ] **Step 5: Fix the two other references to the deleted routes**

`src/app/login/page.tsx:16` and `src/app/dashboard/settings/page.tsx:15` both
point at `/onboarding/creating`. In `login/page.tsx`, change `mockSignIn`:

```tsx
  function mockSignIn() {
    signIn();
    router.replace(isOnboarded() ? "/dashboard" : "/onboarding/mail");
  }
```

This branch only runs when there is no backend, so it skips the connect step —
there is nothing to connect.

In `dashboard/settings/page.tsx`, "Replay onboarding" only ever cleared the local
flag, which no longer decides anything once a backend is configured. Change
`replay` to route to the new first step and hide the card when it cannot work:

```tsx
  function replay() {
    resetOnboarding();
    router.replace("/onboarding/mail");
  }
```

Then wrap the "Replay onboarding" `<Card>` (the whole element, lines ~44–54) so
it only renders on the mock path:

```tsx
          {backendConfigured() ? null : (
            <Card className="flex items-center justify-between p-5">
              {/* ...unchanged contents... */}
            </Card>
          )}
```

`backendConfigured` is already imported in that file. Replaying a
backend-backed account would need an endpoint to clear `onboarded_at`, which this
change does not add.

- [ ] **Step 6: Delete the mock pages**

```bash
git rm -r src/app/onboarding/calendar src/app/onboarding/inbox \
          src/app/onboarding/notes src/app/onboarding/creating
```

`src/components/onboarding/Orbit.tsx` stays — Task 5 uses it.

- [ ] **Step 7: Confirm nothing references the deleted routes**

```bash
grep -rn "onboarding/creating\|onboarding/inbox\|onboarding/notes\|onboarding/calendar" src/
```

Expected: no output.

- [ ] **Step 8: Typecheck, lint, build**

```bash
npx tsc --noEmit && npm run lint && npm run build
```

Expected: all three clean.

- [ ] **Step 9: Manual walk from a clean state**

In the backend repo, clear the flag for your test user:

```bash
docker compose exec postgres psql -U inboxos -d inboxos \
  -c "UPDATE users SET onboarded_at = NULL WHERE email = '<your email>';"
```

Then, in the browser:

1. Open `/dashboard` → expect a bounce to `/onboarding/connect`, with the
   stepper visible and step 1 active.
2. Both accounts already connected → click Continue → `/onboarding/mail`.
3. Try `/dashboard` directly mid-wizard → expect a bounce back to the wizard.
4. Walk the three steps and Finish → land on `/dashboard`.
5. Open `/onboarding/mail` again → expect a redirect to `/dashboard`.
6. Repeat from step 1 choosing Skip on all three steps → expect the same
   landing, with `GET /api/mailman/settings` showing `is_active: false`.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: single onboarding flow, drop the mock pages"
```

---

## Verification summary

**`InboxPilot`:** `uv run ruff check src`, `uv run mypy src`, `make migrate`, and
an `alembic downgrade -1` / `upgrade head` round trip.

**`inboxos-web`:** `npx tsc --noEmit`, `npm run lint`, `npm run build`, plus the
Task 6 Step 8 walk.

## Out of scope

- Outlook. The deleted mock calendar step had a "Continue with Outlook" button
  that did nothing. `User.outlook_sub` exists but no Outlook integration does.
- Redesigning the dashboard settings pages.
- VIP configuration during onboarding — it stays at `/dashboard/mailman`.
- Test infrastructure in either repo.
