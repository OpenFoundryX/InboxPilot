# Dashboard Home Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the placeholder `/dashboard` home page with the reference-screenshot layout, backed by real API data — persisted activity counters, server-derived setup state, and a calendar-backed Today/Tomorrow agenda whose per-meeting toggle really books and recalls the notetaker bot.

**Architecture:** A new append-only `activity_events` table is written by the categorization pipeline and the scheduling-draft path; a new `GET /v1/dashboard/summary` endpoint aggregates it alongside setup state and a calendar-read agenda, bucketed into Today/Tomorrow server-side using the user's stored timezone. The Next.js dashboard page is rewritten around that single fetch. A new `POST /v1/meetings/bot` turns the notetaker on for a calendar event; the existing `DELETE /v1/meetings/{id}/bot` turns it off.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Alembic, Celery, PostgreSQL, Composio (Gmail + Google Calendar); Next.js 14 App Router, React 18, TypeScript, Tailwind.

**Spec:** `docs/superpowers/specs/2026-07-28-dashboard-home-design.md`

## Global Constraints

- **Two repositories.** Backend is `/Users/abcom/Desktop/openfoundry/InboxPilot` (branch `feat/categorization-api`). Frontend is the sibling `/Users/abcom/Desktop/openfoundry/inboxos-web`. They are separate git repos — commit in each independently, and never `cd` between them inside one command.
- **No automated tests are written in this plan.** This is a deliberate decision recorded in the spec, not an oversight. The repo declares `testpaths = ["tests"]` in `pyproject.toml` but has no `tests/` directory and no fixture harness. **Do not create one.** Every task below ends with an explicit non-test verification step instead of a red-green cycle.
- **Pre-existing lint failures.** `uv run ruff check src` currently reports exactly 3 errors, all pre-existing on this branch:
  - `src/models/mailman.py:12` — F401 `UniqueConstraint` imported but unused
  - `src/services/digest/scheduling.py:76` — F541 f-string without placeholders
  - `src/workers/jobs/routines_sweep.py:16` — F401 `integrations.composio.gmail` imported but unused

  `make lint` additionally fails because `ruff check src tests` names a `tests` directory that does not exist. **The bar for every backend task is "no NEW ruff errors", not "ruff clean".** Task 3 fixes the `scheduling.py:76` one incidentally because it edits that exact line; leave the other two alone.
- **Python 3.12**, `line-length = 100` (ruff), `src/` is a bare import root — modules import as `models.x`, `services.x`, never `src.models.x`.
- **Alembic head is `bb9e0a302824`.** The new migration's `down_revision` must be exactly that string.
- **Models are registered for autogenerate in `alembic/env.py`**, not in `src/models/__init__.py` (which is empty). A new model file must be added there or autogenerate will silently propose dropping the table.
- **Commit messages** use Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`) matching the existing log, and end with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```
- **Tailwind tokens only** on the frontend: `cream` `#F3F1EA`, `card` `#FCFBF7`, `ink` `#1A1D26`, `muted` `#6B7280`, `accent` `#F0562D`, `accent-dark` `#D8451F`. Do not introduce new colours or add to `tailwind.config.ts`.

---

## File Structure

**Backend — create:**

| File | Responsibility |
| --- | --- |
| `src/models/activity.py` | `ActivityEvent` model + kind constants. Nothing else. |
| `src/services/activity/__init__.py` | Empty package marker. |
| `src/services/activity/record.py` | Idempotent, never-fatal recording. Sync API for Celery callers. |
| `src/services/dashboard/__init__.py` | Empty package marker. |
| `src/services/dashboard/summary.py` | Stats aggregation, setup state, day bucketing, bot-state derivation. All the logic. |
| `src/schemas/dashboard.py` | Pydantic response models for the summary payload. |
| `src/api/v1/dashboard.py` | Thin router. Auth, dependency wiring, delegation to the service. |
| `alembic/versions/<hash>_activity_events_and_initial_sync.py` | Table + column. |

**Backend — modify:**

| File | Change |
| --- | --- |
| `src/models/users.py` | Add `initial_sync_at` column. |
| `alembic/env.py` | Register the activity model for autogenerate. |
| `src/api/router.py` | Include the dashboard router. |
| `src/api/v1/meetings.py` | Add `POST /meetings/bot`. |
| `src/schemas/meetings.py` | Add `EnableBotRequest`. |
| `src/services/categorization/pipeline.py` | Record on successful label. |
| `src/services/digest/scheduling.py` | Record on draft created. |
| `src/workers/jobs/sync_last_7_days.py` | Stamp `initial_sync_at`. |

**Frontend — create:** `src/lib/dashboard.ts`, `src/components/app/InboxSetupCard.tsx`, `src/components/app/MeetingRow.tsx`, `src/components/app/SubscribeBanner.tsx`, `src/components/app/InviteTeamBanner.tsx`

**Frontend — modify:** `src/app/dashboard/page.tsx` (rewrite), `src/components/app/MeetingsPanel.tsx` (rewrite), `src/components/app/AskBar.tsx` (restyle), `src/components/app/icons.tsx` (add `UsersIcon`), `src/lib/auth.ts` (drop setup flag)

The split between `summary.py` (all logic) and `dashboard.py` (thin router) matters: it is the pattern every other v1 router follows, and it keeps the day-bucketing and bot-state rules — the only genuinely tricky code here — in one file you can hold in your head.

---

## Task 1: The activity_events table and sync marker

**Files:**
- Create: `src/models/activity.py`
- Modify: `src/models/users.py`
- Modify: `alembic/env.py:14-21`
- Create: `alembic/versions/<generated>_activity_events_and_initial_sync.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `models.activity.ActivityEvent`, and the constants `KIND_EMAIL_CATEGORIZED = "email_categorized"`, `KIND_DRAFT_CREATED = "draft_created"`, `KINDS: frozenset[str]`. Column `User.initial_sync_at: datetime | None`.

- [ ] **Step 1: Create the model**

Create `src/models/activity.py`:

```python
"""Append-only record of what InboxPilot did for a user.

One row per thing worth counting on the dashboard: a message labelled, a reply
drafted. Append-only on purpose — the dashboard reads aggregates today, but a
log leaves the door open to a per-day or per-category breakdown later without
needing a backfill to answer the question.

The unique constraint is what makes the counts trustworthy rather than
decorative. `classify.new_email` declares `max_retries=3`, and
`jobs.sync_last_7_days` is documented as the safe-to-re-run catch-up lever — so
without it, one retry storm or one manual catch-up permanently inflates the
number the user reads on the dashboard.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

KIND_EMAIL_CATEGORIZED = "email_categorized"
KIND_DRAFT_CREATED = "draft_created"
KINDS = frozenset({KIND_EMAIL_CATEGORIZED, KIND_DRAFT_CREATED})


class ActivityEvent(UUIDMixin, TimestampMixin, Base):
    """One dashboard-countable thing that happened, at most once per `ref_id`."""

    __tablename__ = "activity_events"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "ref_id", name="uq_activity_events_user_kind_ref"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    # The Gmail id that makes this event unique: the categorized message for
    # `email_categorized`, and the *source* message for `draft_created` — one
    # email replied to counts once, even if a re-run leaves a second draft
    # object behind in the mailbox.
    ref_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # References EmailCategory.key. Not an FK, for the same reason
    # CategorizationRule.category_key is not one: `key` is unique only per user,
    # so this would need a composite (user_id, key) target. Unlike the rules
    # table it is also never cleaned up when a category is deleted — these rows
    # record what happened, and rewriting history to match a renamed taxonomy
    # would be a lie.
    category_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    def __repr__(self) -> str:
        return f"<ActivityEvent {self.kind} {self.ref_id}>"
```

- [ ] **Step 2: Add the sync marker column**

In `src/models/users.py`, add this field to `User`, directly after the `last_login_at` line:

```python
    # Stamped by `jobs.sync_last_7_days` when onboarding finishes. The dashboard
    # reads it as the sole signal separating "still setting up" from "ready".
    initial_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

`datetime` and `DateTime` are already imported in that file. No new imports.

- [ ] **Step 3: Register the model for autogenerate**

In `alembic/env.py`, add to the alphabetically-sorted import block at lines 14-21, as the new first entry:

```python
from models import activity as activity_models  # noqa: F401
```

Skipping this makes autogenerate propose **dropping** the table it cannot see. Do not skip it.

- [ ] **Step 4: Generate the migration**

Run: `make revision m="add activity events and initial sync marker"`

- [ ] **Step 5: Review and correct the generated migration**

Autogenerate is a starting point, not the answer. Open the new file in `alembic/versions/` and verify by hand:

1. `down_revision` is exactly `'bb9e0a302824'`. Fix it if not.
2. `upgrade()` creates `activity_events` with all seven columns, the FK to `users.id` with `ondelete='CASCADE'`, indexes on `user_id`, `kind`, and `occurred_at`, and the named unique constraint `uq_activity_events_user_kind_ref`.
3. `upgrade()` adds `initial_sync_at` to `users` as nullable.
4. `downgrade()` drops the column and the table, and contains **no** unrelated operations. If autogenerate has proposed dropping or altering any other table, delete those lines — they are drift, not part of this change.

- [ ] **Step 6: Apply the migration**

Run: `make migrate`
Expected: `Running upgrade bb9e0a302824 -> <new hash>` and no error.

- [ ] **Step 7: Verify the schema landed**

Run: `docker compose exec postgres psql -U inboxos -d inboxos -c "\d activity_events"`

Expected: the table prints with the seven columns, and the constraint list includes `"uq_activity_events_user_kind_ref" UNIQUE CONSTRAINT, btree (user_id, kind, ref_id)`.

If the postgres user or database name differs in your `.env`, read `POSTGRES_USER` / `POSTGRES_DB` from it and substitute.

- [ ] **Step 8: Check for new lint errors**

Run: `uv run ruff check src`
Expected: still exactly the 3 pre-existing errors listed in Global Constraints. Any 4th error is yours — fix it.

- [ ] **Step 9: Commit**

```bash
git add src/models/activity.py src/models/users.py alembic/env.py alembic/versions/
git commit -m "$(cat <<'EOF'
feat: record dashboard activity in an append-only table

activity_events gets one row per categorized message and per drafted
reply. The unique constraint on (user_id, kind, ref_id) is what makes the
counts trustworthy: classify.new_email retries three times and
sync_last_7_days is the documented catch-up lever, so without it a retry
storm permanently inflates what the dashboard reports.

users.initial_sync_at is the marker separating "still onboarding" from
"ready", replacing a localStorage flag on the client.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: The recording service

**Files:**
- Create: `src/services/activity/__init__.py`
- Create: `src/services/activity/record.py`

**Interfaces:**
- Consumes: `models.activity.ActivityEvent`, `KIND_EMAIL_CATEGORIZED`, `KIND_DRAFT_CREATED` (Task 1).
- Produces: `services.activity.record.record_email_categorized(user_id: str, message_id: str, category_key: str) -> None` and `services.activity.record.record_draft_created(user_id: str, source_message_id: str) -> None`. Both synchronous, both return `None`, and **neither ever raises**.

- [ ] **Step 1: Create the package marker**

Create `src/services/activity/__init__.py` as an empty file. (Check the sibling `src/services/categorization/__init__.py` — if it carries a docstring, match that convention instead.)

- [ ] **Step 2: Write the recording module**

Create `src/services/activity/record.py`:

```python
"""Record dashboard-countable activity. Idempotent, and never fatal to callers.

Both entry points are synchronous because every caller is Celery code. They
bridge to the async DB layer with `run_async(with_worker_session(...))` — the
same pattern `services.categorization.pipeline.get_config` uses, which exists
because a pooled connection from an earlier event loop makes asyncpg raise
"attached to a different loop".
"""

import uuid

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import run_async, with_worker_session
from core.logging import get_logger
from models.activity import KIND_DRAFT_CREATED, KIND_EMAIL_CATEGORIZED, ActivityEvent

log = get_logger(__name__)


def record_email_categorized(user_id: str, message_id: str, category_key: str) -> None:
    """Count one message as categorized. Call only after the label has landed."""
    _record(user_id, KIND_EMAIL_CATEGORIZED, message_id, category_key)


def record_draft_created(user_id: str, source_message_id: str) -> None:
    """Count one drafted reply, keyed by the email being replied to."""
    _record(user_id, KIND_DRAFT_CREATED, source_message_id, None)


def _record(user_id: str, kind: str, ref_id: str, category_key: str | None) -> None:
    """Insert one event, ignoring duplicates.

    Swallows every error by design. A statistic must never fail the work it
    describes: raising here would retry a Celery task that already labelled the
    mail successfully, re-doing Gmail work to fix a counter.
    """
    try:
        run_async(
            with_worker_session(lambda db: _insert(db, user_id, kind, ref_id, category_key))
        )
    except Exception:
        log.exception("activity.record_failed", user_id=user_id, kind=kind, ref_id=ref_id)


async def _insert(
    db: AsyncSession, user_id: str, kind: str, ref_id: str, category_key: str | None
) -> None:
    stmt = (
        insert(ActivityEvent)
        .values(
            id=uuid.uuid4(),
            user_id=uuid.UUID(user_id),
            kind=kind,
            ref_id=ref_id,
            category_key=category_key,
        )
        .on_conflict_do_nothing(constraint="uq_activity_events_user_kind_ref")
    )
    await db.execute(stmt)
```

`occurred_at`, `created_at`, and `updated_at` all carry `server_default`s, so omitting them from `.values()` is correct — Postgres fills them.

- [ ] **Step 3: Verify recording and idempotency by hand**

There is no test suite, so exercise it directly. Get a real user id first:

```bash
docker compose exec postgres psql -U inboxos -d inboxos -t -c "SELECT id FROM users LIMIT 1;"
```

Then, substituting that id:

```bash
docker compose run --rm api python -c "
from services.activity.record import record_email_categorized
uid = 'PASTE_USER_ID_HERE'
record_email_categorized(uid, 'test-msg-1', 'to_do')
record_email_categorized(uid, 'test-msg-1', 'to_do')
record_email_categorized(uid, 'test-msg-2', 'fyi')
"
```

- [ ] **Step 4: Confirm the duplicate was swallowed**

Run:

```bash
docker compose exec postgres psql -U inboxos -d inboxos -c \
  "SELECT kind, ref_id, category_key FROM activity_events ORDER BY ref_id;"
```

Expected: exactly **two** rows — `test-msg-1` and `test-msg-2`. Three rows means `on_conflict_do_nothing` is not binding to the constraint; check the constraint name matches Task 1 exactly.

- [ ] **Step 5: Clean up the probe rows**

Run:

```bash
docker compose exec postgres psql -U inboxos -d inboxos -c \
  "DELETE FROM activity_events WHERE ref_id LIKE 'test-msg-%';"
```

- [ ] **Step 6: Check for new lint errors**

Run: `uv run ruff check src && uv run mypy src`
Expected: ruff still reports exactly the 3 pre-existing errors. Note whether mypy's output changed from its pre-task baseline — capture that baseline with `uv run mypy src` **before** editing if you have not already.

- [ ] **Step 7: Commit**

```bash
git add src/services/activity/
git commit -m "$(cat <<'EOF'
feat: idempotent recording for dashboard activity

Sync entry points for Celery callers, bridging to the async DB layer the
way pipeline.get_config already does. Errors are swallowed and logged on
purpose: a counter must never fail the categorization it describes, or a
retry would re-do Gmail work to fix a statistic.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Wire recording into the pipelines

**Files:**
- Modify: `src/services/categorization/pipeline.py:165-175`
- Modify: `src/services/digest/scheduling.py:76-85`
- Modify: `src/workers/jobs/sync_last_7_days.py`

**Interfaces:**
- Consumes: `record_email_categorized`, `record_draft_created` (Task 2); `User.initial_sync_at` (Task 1).
- Produces: no new callable surface. After this task the counters and the setup marker have real data flowing into them.

- [ ] **Step 1: Record on successful categorization**

In `src/services/categorization/pipeline.py`, add the import alongside the other `services` imports at the top:

```python
from services.activity.record import record_email_categorized
```

Then in `categorize_and_apply`, the tail of the function currently reads:

```python
    gmail_ops.apply_category(
        user_id, [message_id], category.gmail_label, category.actions
    )
    log.info(
        "categorize.applied",
        user_id=user_id,
        message_id=message_id,
        category=category.key,
        matched_rule=rule is not None,
        actions=category.actions,
    )
    return category.key
```

Insert the recording call between `apply_category` and `log.info`:

```python
    gmail_ops.apply_category(
        user_id, [message_id], category.gmail_label, category.actions
    )
    # After the label lands, never before: a Gmail failure raises above this
    # line, so a message we failed to label is never counted as categorized.
    record_email_categorized(user_id, message_id, category.key)
    log.info(
        "categorize.applied",
        user_id=user_id,
        message_id=message_id,
        category=category.key,
        matched_rule=rule is not None,
        actions=category.actions,
    )
    return category.key
```

Leave every earlier `return None` path untouched. Disabled, excluded-by-rule, missing-category, and below-threshold-with-no-fallback all applied no label, so all correctly count nothing.

- [ ] **Step 2: Record on drafted replies**

In `src/services/digest/scheduling.py`, add the import alongside the other `services` imports:

```python
from services.activity.record import record_draft_created
```

The call site currently reads:

```python
        body = f"Hi,\n\nHappy to find a time. Here are a few slots that work on my end:\n\n" + calendar.format_slots(slots) + "\n\nLet me know what suits you and I'll send an invite.\n\nBest"
        subject = f"Re: {e.subject or 'Meeting'}"
        try:
            gmail.create_draft(user_id, requester, subject, body, thread_id=e.thread_id)
            gmail_ops.add_label(user_id, [e.id], SCHEDULED_LABEL)
        except Exception:
            log.exception("scheduling.draft_failed", message_id=e.id)
            continue
        drafted += 1
```

Replace that block with:

```python
        body = "Hi,\n\nHappy to find a time. Here are a few slots that work on my end:\n\n" + calendar.format_slots(slots) + "\n\nLet me know what suits you and I'll send an invite.\n\nBest"
        subject = f"Re: {e.subject or 'Meeting'}"
        try:
            gmail.create_draft(user_id, requester, subject, body, thread_id=e.thread_id)
            gmail_ops.add_label(user_id, [e.id], SCHEDULED_LABEL)
        except Exception:
            log.exception("scheduling.draft_failed", message_id=e.id)
            continue
        # Keyed by the source message, not the draft id: one email replied to
        # counts once, even if a re-run leaves a second draft object behind.
        record_draft_created(user_id, e.id)
        drafted += 1
```

Two changes in one block: the `f` prefix is dropped from `body` (it has no placeholders — this clears the pre-existing F541 on this exact line, which is fair game because the task edits it anyway), and the recording call goes on the success path after the `except/continue`.

- [ ] **Step 3: Stamp the sync marker**

In `src/workers/jobs/sync_last_7_days.py`, add these imports:

```python
import uuid
from datetime import datetime, timezone

from core.database import run_async, with_worker_session
from models.users import User
```

Add this module-level helper below `_install_trigger`:

```python
def _stamp_initial_sync(user_id: str) -> None:
    """Mark onboarding complete. Guarded like the other DB work in this task:
    a failed stamp must not lose the trigger install or the classified mail."""

    async def _write(db) -> None:
        user = await db.get(User, uuid.UUID(user_id))
        if user is not None and user.initial_sync_at is None:
            user.initial_sync_at = datetime.now(timezone.utc)

    try:
        run_async(with_worker_session(_write))
    except Exception:
        log.exception("gmail.initial_sync_stamp_failed", user_id=user_id)
```

The `initial_sync_at is None` check keeps the timestamp meaning *first* completion — a later catch-up run must not move it forward.

Then in the `sync_last_7_days` task body, call it immediately after the `_install_trigger` line and before the `log.info("gmail.sync_last_7_days", ...)` call:

```python
    trigger_id, trigger_error = _install_trigger(user_id)
    _stamp_initial_sync(user_id)
```

- [ ] **Step 4: Verify the categorization path end to end**

With the stack running, trigger a classification of a real message. Get a message id from a recent log line, or re-run the backfill for one user:

```bash
docker compose run --rm api python -c "
from workers.jobs.sync_last_7_days import sync_last_7_days
sync_last_7_days('PASTE_USER_ID_HERE', days=2, max_results=5)
"
```

Then:

```bash
docker compose exec postgres psql -U inboxos -d inboxos -c \
  "SELECT kind, count(*) FROM activity_events GROUP BY kind;"
docker compose exec postgres psql -U inboxos -d inboxos -c \
  "SELECT id, initial_sync_at FROM users;"
```

Expected: `email_categorized` has a non-zero count, and that user's `initial_sync_at` is set.

- [ ] **Step 5: Verify re-running does not double count**

Note the exact count from Step 4, then run the identical `sync_last_7_days` command a second time and re-check:

```bash
docker compose exec postgres psql -U inboxos -d inboxos -c \
  "SELECT kind, count(*) FROM activity_events GROUP BY kind;"
```

Expected: **the same count**. This is the single most important check in the plan — it is the whole reason the unique constraint exists. Also confirm `initial_sync_at` still holds its original timestamp and has not moved.

- [ ] **Step 6: Check lint**

Run: `uv run ruff check src`
Expected: **2** errors now, not 3 — `scheduling.py:76` is fixed by Step 2. The `mailman.py` and `routines_sweep.py` F401s remain, untouched.

- [ ] **Step 7: Commit**

```bash
git add src/services/categorization/pipeline.py src/services/digest/scheduling.py src/workers/jobs/sync_last_7_days.py
git commit -m "$(cat <<'EOF'
feat: feed the activity log from categorization and drafting

Categorization records after apply_category returns, so mail we failed to
label is never counted. Scheduling drafts record against the source
message id, so one email replied to counts once regardless of how many
draft objects a re-run leaves behind. sync_last_7_days stamps
initial_sync_at once, and never moves it on a catch-up run.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: The summary endpoint

**Files:**
- Create: `src/services/dashboard/__init__.py`
- Create: `src/services/dashboard/summary.py`
- Create: `src/schemas/dashboard.py`
- Create: `src/api/v1/dashboard.py`
- Modify: `src/api/router.py`

**Interfaces:**
- Consumes: `ActivityEvent`, kind constants, `User.initial_sync_at` (Task 1); `integrations.composio.calendar.list_events(user_id: str, time_min: datetime, time_max: datetime) -> list[dict]`; `services.meetings.rules.event_bounds(event: dict) -> tuple[datetime, datetime] | None`; `services.meetings.links.link_from_event(event: dict) -> tuple[str, str] | None`; `services.mailman.store.get_or_create_settings(db, user_id) -> MailmanSettings`; `models.meetings` status constants.
- Produces:
  - `services.dashboard.summary.first_name(user: User) -> str`
  - `services.dashboard.summary.setup_state(user: User) -> str` returning `"syncing"` or `"ready"`
  - `services.dashboard.summary.load_stats(db: AsyncSession, user_id: uuid.UUID) -> DashboardStats`
  - `services.dashboard.summary.day_bounds(tz_name: str, now: datetime | None = None) -> tuple[datetime, datetime, datetime]` returning `(today_start, tomorrow_start, day_after_start)` as UTC instants
  - `services.dashboard.summary.bot_flags(meeting: Meeting | None, starts_at: datetime, has_link: bool, now: datetime) -> tuple[bool, bool]` returning `(bot_on, bot_editable)`
  - `services.dashboard.summary.load_agenda(db: AsyncSession, user_id: uuid.UUID, tz_name: str) -> DashboardMeetings`
  - `services.dashboard.summary.build_summary(db: AsyncSession, user: User) -> DashboardSummary`
  - `services.dashboard.summary.SETUP_SYNCING`, `SETUP_READY`
  - Schemas `DashboardUser`, `DashboardSetup`, `DashboardStats`, `AgendaItem`, `DashboardMeetings`, `DashboardSummary` in `schemas.dashboard`
  - Endpoint `GET /v1/dashboard/summary`

- [ ] **Step 1: Create the schemas**

Create `src/schemas/dashboard.py`:

```python
"""Response schemas for the dashboard home payload."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class DashboardUser(BaseModel):
    first_name: str


class DashboardSetup(BaseModel):
    # "syncing" until onboarding finishes, "ready" after.
    state: str
    initial_sync_at: datetime | None = None


class DashboardStats(BaseModel):
    emails_categorized: int
    drafts_created: int


class AgendaItem(BaseModel):
    calendar_event_id: str
    meeting_id: uuid.UUID | None = None
    title: str | None = None
    starts_at: datetime
    ends_at: datetime
    meeting_url: str | None = None
    # False when no bot is booked, or the booking was cancelled or failed.
    bot_on: bool
    # False once the call is underway or over — the UI disables rather than
    # offering a toggle the API would reject.
    bot_editable: bool


class DashboardMeetings(BaseModel):
    timezone: str
    today: list[AgendaItem] = []
    tomorrow: list[AgendaItem] = []


class DashboardSummary(BaseModel):
    user: DashboardUser
    setup: DashboardSetup
    stats: DashboardStats
    meetings: DashboardMeetings
```

- [ ] **Step 2: Create the package marker**

Create `src/services/dashboard/__init__.py` as an empty file, matching whatever convention `src/services/categorization/__init__.py` uses.

- [ ] **Step 3: Write the summary service**

Create `src/services/dashboard/summary.py`:

```python
"""Assembles the dashboard home payload.

All the logic lives here so the router stays thin, matching every other v1
router in this codebase.
"""

import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from integrations.composio import calendar
from models.activity import KIND_DRAFT_CREATED, KIND_EMAIL_CATEGORIZED, ActivityEvent
from models.meetings import (
    STATUS_CANCELLED,
    STATUS_DELIVERED,
    STATUS_ENDED,
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_RECORDED,
    STATUS_RECORDING,
    Meeting,
)
from models.users import User
from schemas.dashboard import (
    AgendaItem,
    DashboardMeetings,
    DashboardSetup,
    DashboardStats,
    DashboardSummary,
    DashboardUser,
)
from services.mailman.store import get_or_create_settings
from services.meetings.links import link_from_event
from services.meetings.rules import event_bounds

log = get_logger(__name__)

SETUP_SYNCING = "syncing"
SETUP_READY = "ready"

# No bot is attending: either none was ever booked, or the booking is gone.
BOT_OFF_STATUSES = frozenset({STATUS_CANCELLED, STATUS_FAILED})

# The call has started or finished — nothing left to toggle. Mirrors the 409
# condition in POST /v1/meetings/bot; the two must be kept in step.
BOT_LOCKED_STATUSES = frozenset(
    {STATUS_RECORDING, STATUS_ENDED, STATUS_RECORDED, STATUS_PROCESSED, STATUS_DELIVERED}
)


def first_name(user: User) -> str:
    """The name to greet by: first token of full_name, else the email local part."""
    if user.full_name:
        parts = user.full_name.split()
        if parts:
            return parts[0]
    return user.email.split("@")[0]


def setup_state(user: User) -> str:
    """Derived from the DB alone — no Composio round trip on the dashboard path.

    DashboardLayout already gates on Gmail and Calendar being connected and
    redirects to /onboarding/connect otherwise, so a request that reaches this
    endpoint has established connectivity. Re-verifying would add a blocking
    third-party call per page load to restate a known fact.
    """
    return SETUP_READY if user.initial_sync_at else SETUP_SYNCING


async def load_stats(db: AsyncSession, user_id: uuid.UUID) -> DashboardStats:
    """Lifetime totals. One grouped query; absent kinds report zero."""
    rows = await db.execute(
        select(ActivityEvent.kind, func.count())
        .where(ActivityEvent.user_id == user_id)
        .group_by(ActivityEvent.kind)
    )
    counts = {kind: total for kind, total in rows.all()}
    return DashboardStats(
        emails_categorized=counts.get(KIND_EMAIL_CATEGORIZED, 0),
        drafts_created=counts.get(KIND_DRAFT_CREATED, 0),
    )


def day_bounds(tz_name: str, now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """Local midnight today, tomorrow, and the day after — returned as UTC instants.

    Built from calendar dates rather than by adding 24-hour deltas: on a DST
    transition day the local day is 23 or 25 hours long, and `midnight + 1 day`
    would land an hour either side of midnight rather than on it.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.warning("dashboard.unknown_timezone", timezone=tz_name)
        tz = ZoneInfo("UTC")

    local_today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    midnights = [
        datetime.combine(local_today + timedelta(days=offset), time.min, tzinfo=tz)
        for offset in (0, 1, 2)
    ]
    return tuple(m.astimezone(timezone.utc) for m in midnights)  # type: ignore[return-value]


def bot_flags(
    meeting: Meeting | None, starts_at: datetime, has_link: bool, now: datetime
) -> tuple[bool, bool]:
    """(bot_on, bot_editable) for one agenda row."""
    bot_on = meeting is not None and meeting.status not in BOT_OFF_STATUSES

    if not has_link:
        # Part of the user's day, but there is nothing for a bot to join.
        return False, False
    if starts_at <= now:
        return bot_on, False
    if meeting is not None and meeting.status in BOT_LOCKED_STATUSES:
        return bot_on, False
    return bot_on, True


async def load_agenda(db: AsyncSession, user_id: uuid.UUID, tz_name: str) -> DashboardMeetings:
    """The user's next two days, from the calendar, annotated with bot state.

    Read from the calendar rather than the meetings table on purpose: the table
    holds only calls the sweep chose to book, so an event the notetaker skips —
    "deep work (no calls please)" — has no row, and an agenda built from the
    table alone could never show it as Off.

    A calendar outage empties the agenda but must not empty the page: the stats
    card has nothing to do with Google being reachable.
    """
    now = datetime.now(timezone.utc)
    today_start, tomorrow_start, day_after_start = day_bounds(tz_name, now)

    try:
        events = await run_in_threadpool(
            calendar.list_events, str(user_id), today_start, day_after_start
        )
    except Exception:
        log.exception("dashboard.calendar_unavailable", user_id=str(user_id))
        return DashboardMeetings(timezone=tz_name, today=[], tomorrow=[])

    rows = await db.scalars(select(Meeting).where(Meeting.user_id == user_id))
    by_event = {m.calendar_event_id: m for m in rows if m.calendar_event_id}

    today: list[AgendaItem] = []
    tomorrow: list[AgendaItem] = []

    for event in events:
        event_id = event.get("id")
        bounds = event_bounds(event)
        # All-day entries have no dateTime and no place on a timed agenda.
        if not event_id or not bounds:
            continue
        starts_at, ends_at = bounds

        meeting = by_event.get(str(event_id))
        link = link_from_event(event)
        bot_on, bot_editable = bot_flags(meeting, starts_at, link is not None, now)

        item = AgendaItem(
            calendar_event_id=str(event_id),
            meeting_id=meeting.id if meeting else None,
            title=event.get("summary"),
            starts_at=starts_at,
            ends_at=ends_at,
            meeting_url=link[0] if link else None,
            bot_on=bot_on,
            bot_editable=bot_editable,
        )

        if starts_at < tomorrow_start:
            today.append(item)
        elif starts_at < day_after_start:
            tomorrow.append(item)

    today.sort(key=lambda i: i.starts_at)
    tomorrow.sort(key=lambda i: i.starts_at)
    return DashboardMeetings(timezone=tz_name, today=today, tomorrow=tomorrow)


async def build_summary(db: AsyncSession, user: User) -> DashboardSummary:
    stats = await load_stats(db, user.id)
    mailman_settings = await get_or_create_settings(db, user.id)
    meetings = await load_agenda(db, user.id, mailman_settings.timezone or "UTC")
    return DashboardSummary(
        user=DashboardUser(first_name=first_name(user)),
        setup=DashboardSetup(state=setup_state(user), initial_sync_at=user.initial_sync_at),
        stats=stats,
        meetings=meetings,
    )
```

- [ ] **Step 4: Write the router**

Create `src/api/v1/dashboard.py`:

```python
"""Dashboard home — one aggregate payload for the landing page."""

from typing import Annotated

from fastapi import APIRouter, Depends

from api.deps import DbSession
from models.users import User
from schemas.dashboard import DashboardSummary
from services.auth.dependencies import get_current_user
from services.dashboard import summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(user: CurrentUser, db: DbSession) -> DashboardSummary:
    """Everything the dashboard home renders, in one round trip.

    One endpoint rather than a client-side fan-out so the server owns day
    boundaries: the browser's clock must not get to disagree with the backend
    about what "tomorrow" means.
    """
    return await summary.build_summary(db, user)
```

- [ ] **Step 5: Register the router**

In `src/api/router.py`, add `dashboard` to the import list and include it. The import line becomes:

```python
from api.v1 import auth, categorization, chat, dashboard, integrations, mailman, users, webhooks, meetings
```

And add this line after `api_router.include_router(chat.router)`:

```python
api_router.include_router(dashboard.router)
```

- [ ] **Step 6: Verify the endpoint responds**

Restart the API (`docker compose restart api`), then open `http://localhost:8000/docs` and confirm a **dashboard** tag with `GET /v1/dashboard/summary` appears.

Call it with a real session cookie from a logged-in browser, or via the docs "Try it out" button while authenticated.

Expected shape:

```json
{
  "user": { "first_name": "Nilesh" },
  "setup": { "state": "ready", "initial_sync_at": "2026-07-26T09:12:00Z" },
  "stats": { "emails_categorized": 12, "drafts_created": 0 },
  "meetings": { "timezone": "Asia/Kolkata", "today": [], "tomorrow": [] }
}
```

- [ ] **Step 7: Verify the counts are real**

Cross-check the number against the table directly:

```bash
docker compose exec postgres psql -U inboxos -d inboxos -c \
  "SELECT kind, count(*) FROM activity_events GROUP BY kind;"
```

Expected: matches `stats` in the JSON exactly.

- [ ] **Step 8: Verify day bucketing against a non-UTC timezone**

`day_bounds` is a pure function, so exercise it directly without a database:

```bash
docker compose run --rm api python -c "
from datetime import datetime, timezone
from services.dashboard.summary import day_bounds
now = datetime(2026, 7, 28, 20, 30, tzinfo=timezone.utc)  # 02:00 on the 29th in IST
print([b.isoformat() for b in day_bounds('Asia/Kolkata', now)])
print([b.isoformat() for b in day_bounds('UTC', now)])
print([b.isoformat() for b in day_bounds('Not/AZone', now)])
"
```

Expected:
- `Asia/Kolkata` → `2026-07-28T18:30:00+00:00`, `2026-07-29T18:30:00+00:00`, `2026-07-30T18:30:00+00:00`. The user is already on the 29th locally, so "today" starts the previous UTC evening.
- `UTC` → three clean midnights on the 28th, 29th, 30th.
- `Not/AZone` → falls back to the UTC values rather than raising.

- [ ] **Step 9: Verify the agenda against a real calendar**

Call `GET /v1/dashboard/summary` again while authenticated, with at least one event on today's calendar.

Check each of these:
1. Events land in `today` vs `tomorrow` matching what the user's own calendar shows in their local timezone.
2. An event with a Meet/Zoom/Teams link has a non-null `meeting_url`.
3. An event with **no** link has `bot_on: false` and `bot_editable: false`.
4. A past event from earlier today has `bot_editable: false`.
5. `timezone` echoes the user's `mailman_settings.timezone`.

- [ ] **Step 10: Verify a calendar failure degrades gracefully**

Temporarily break the calendar call to confirm the page survives it — in `load_agenda`, add `raise RuntimeError("boom")` as the first line of the `try` block, restart the API, and call the endpoint.

Expected: HTTP **200**, with `stats` and `setup` fully populated and `today`/`tomorrow` empty. A 500 here means the `except` is not wrapping the call correctly.

**Remove the `raise` line before continuing.**

- [ ] **Step 11: Check lint**

Run: `uv run ruff check src`
Expected: still the 2 remaining pre-existing errors, no new ones.

- [ ] **Step 12: Commit**

```bash
git add src/services/dashboard/ src/schemas/dashboard.py src/api/v1/dashboard.py src/api/router.py
git commit -m "$(cat <<'EOF'
feat: GET /v1/dashboard/summary

One aggregate endpoint rather than a client-side fan-out, so the server
owns day boundaries and the page has one loading state. Setup state comes
from users.initial_sync_at alone — the layout already gates on Gmail being
connected, so re-checking would cost a Composio call per page load to
restate a known fact.

The agenda reads from the calendar rather than the meetings table: that
table holds only calls the sweep chose to book, so an event the notetaker
skips has no row and could never render as Off. Day boundaries come from
the user's stored timezone and are built from calendar dates, not 24-hour
deltas, so a DST transition day still buckets on local midnight. A
calendar outage empties the agenda and leaves the rest of the page intact.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Turning the notetaker on for a calendar event

**Files:**
- Modify: `src/schemas/meetings.py`
- Modify: `src/api/v1/meetings.py`

**Interfaces:**
- Consumes: `store.upsert_from_event(db, user_id, event) -> tuple[Meeting, bool]`; `calendar.list_events`; `link_from_event`; `join_now.delay`; the `BOT_LOCKED_STATUSES` rule from Task 4.
- Produces: `POST /v1/meetings/bot` accepting `{"calendar_event_id": str}`, returning `MeetingRead` with `202 Accepted`.

- [ ] **Step 1: Add the request schema**

In `src/schemas/meetings.py`, add after `JoinRequest`:

```python
class EnableBotRequest(BaseModel):
    """Turn the notetaker on for an event already on the user's calendar.

    Covers both "never booked" and "booked then cancelled" — the calendar event
    is the stable identifier, since the Meeting row may not exist yet.
    """

    calendar_event_id: str = Field(min_length=1)
```

- [ ] **Step 2: Add the endpoint**

In `src/api/v1/meetings.py`, extend the imports:

```python
from datetime import datetime, timedelta, timezone

from integrations.composio import calendar
from models.meetings import (
    ACTIVE_STATUSES,
    SOURCE_ADHOC,
    STATUS_CANCELLED,
    STATUS_DELIVERED,
    STATUS_ENDED,
    STATUS_PENDING,
    STATUS_PROCESSED,
    STATUS_RECORDED,
    STATUS_RECORDING,
    Meeting,
    MeetingSettings,
)
from schemas.meetings import (
    EnableBotRequest,
    JoinRequest,
    MeetingDetail,
    MeetingRead,
    SettingsRead,
    SettingsUpdate,
)
from services.meetings.links import find_meeting_link, link_from_event
from services.meetings.store import get_or_create_settings, upsert_from_event
```

Add a module-level constant beside `LIST_LIMIT`:

```python
# Matches BOT_LOCKED_STATUSES in services.dashboard.summary — the agenda's
# `bot_editable` flag exists so the UI never provokes the 409 below.
LOCKED_STATUSES = (
    STATUS_RECORDING,
    STATUS_ENDED,
    STATUS_RECORDED,
    STATUS_PROCESSED,
    STATUS_DELIVERED,
)
# How far ahead to look for the event, matching meetings_sweep's horizon.
EVENT_LOOKUP_HOURS = 48
```

Add the endpoint immediately **before** the existing `cancel_bot` handler, so the on/off pair reads together:

```python
@router.post("/bot", response_model=MeetingRead, status_code=status.HTTP_202_ACCEPTED)
async def enable_bot(payload: EnableBotRequest, user: CurrentUser, db: DbSession) -> Meeting:
    """Turn the notetaker on for a calendar event.

    One path covers both "never booked" and "previously cancelled": the row is
    upserted from the calendar event either way. Its counterpart is
    DELETE /meetings/{id}/bot, which already handles turning it off.
    """
    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "The notetaker is disabled")

    now = datetime.now(timezone.utc)
    events = await run_in_threadpool(
        calendar.list_events, str(user.id), now, now + timedelta(hours=EVENT_LOOKUP_HOURS)
    )
    event = next((e for e in events if str(e.get("id")) == payload.calendar_event_id), None)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That event is not on your calendar")

    if not link_from_event(event):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That event has no Zoom, Google Meet, or Teams link to join",
        )

    meeting, _ = await upsert_from_event(db, user.id, event)
    if meeting.status in LOCKED_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Too late: meeting is {meeting.status}"
        )
    if meeting.starts_at and meeting.starts_at <= now:
        raise HTTPException(status.HTTP_409_CONFLICT, "That meeting has already started")

    meeting.status = STATUS_PENDING
    meeting.status_detail = None
    await db.flush()

    join_now.delay(str(meeting.id))
    log.info("meetings.bot_enabled", user_id=str(user.id), meeting_id=str(meeting.id))
    return meeting
```

- [ ] **Step 3: Verify the happy path**

With an upcoming linked meeting on the calendar, first read `GET /v1/dashboard/summary` and copy a `calendar_event_id` from `today` or `tomorrow` where `bot_editable` is `true`.

Then `POST /v1/meetings/bot` with `{"calendar_event_id": "<that id>"}`.

Expected: `202`, a `MeetingRead` body with `status` `pending` or `scheduled`. Re-reading the summary shows that row with `bot_on: true`.

- [ ] **Step 4: Verify the off/on round trip**

`DELETE /v1/meetings/{meeting_id}/bot` using the `meeting_id` from the previous response.

Expected: `200`, `status` `cancelled`. The summary now shows `bot_on: false` and `bot_editable: true` for that row.

Then `POST /v1/meetings/bot` with the same `calendar_event_id` again.

Expected: `202`, back to `bot_on: true`. This round trip is the whole point of the endpoint — a cancelled meeting must be re-bookable.

- [ ] **Step 5: Verify the guards**

Three checks:
1. `POST /v1/meetings/bot` with `{"calendar_event_id": "does-not-exist"}` → **404**, `"That event is not on your calendar"`.
2. Pick a calendar event with no video link and post its id → **422**, mentioning Zoom/Meet/Teams.
3. `PUT /v1/meetings/settings` with `{"enabled": false}`, then post any valid event id → **409**, `"The notetaker is disabled"`. Set `enabled` back to `true` afterwards.

- [ ] **Step 6: Check lint**

Run: `uv run ruff check src`
Expected: the 2 pre-existing errors, no new ones. Watch for an unused-import warning if you added a status constant the file does not end up using — remove any that ruff flags.

- [ ] **Step 7: Commit**

```bash
git add src/api/v1/meetings.py src/schemas/meetings.py
git commit -m "$(cat <<'EOF'
feat: POST /v1/meetings/bot to turn the notetaker on

Keyed by calendar event rather than meeting id, because the row may not
exist yet — one path then covers both "never booked" and "booked then
cancelled". DELETE /meetings/{id}/bot already handles the other direction.

The 409 conditions mirror the agenda's bot_editable flag exactly, so the
UI disables the toggle instead of provoking the error.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Frontend API client

**Files:**
- Create: `inboxos-web/src/lib/dashboard.ts`
- Modify: `inboxos-web/src/components/app/icons.tsx`

**Interfaces:**
- Consumes: `apiFetch` from `@/lib/api`; the payload shape from Tasks 4-5.
- Produces:
  - Types `DashboardSummary`, `AgendaItem`, `DashboardStats`, `DashboardSetup`, `DashboardMeetings`
  - `getDashboardSummary(): Promise<DashboardSummary>`
  - `enableMeetingBot(calendarEventId: string): Promise<void>`
  - `disableMeetingBot(meetingId: string): Promise<void>`
  - `formatTimeRange(startsAt: string, endsAt: string, timeZone: string): string`
  - `UsersIcon` exported from `@/components/app/icons`

All remaining tasks run in `/Users/abcom/Desktop/openfoundry/inboxos-web`.

- [ ] **Step 1: Write the client module**

Create `src/lib/dashboard.ts`:

```ts
import { apiFetch } from "./api";

export type SetupState = "syncing" | "ready";

export type DashboardSetup = {
  state: SetupState;
  initial_sync_at: string | null;
};

export type DashboardStats = {
  emails_categorized: number;
  drafts_created: number;
};

export type AgendaItem = {
  calendar_event_id: string;
  meeting_id: string | null;
  title: string | null;
  starts_at: string;
  ends_at: string;
  meeting_url: string | null;
  bot_on: boolean;
  bot_editable: boolean;
};

export type DashboardMeetings = {
  timezone: string;
  today: AgendaItem[];
  tomorrow: AgendaItem[];
};

export type DashboardSummary = {
  user: { first_name: string };
  setup: DashboardSetup;
  stats: DashboardStats;
  meetings: DashboardMeetings;
};

export function getDashboardSummary(): Promise<DashboardSummary> {
  return apiFetch<DashboardSummary>("/dashboard/summary");
}

export function enableMeetingBot(calendarEventId: string): Promise<void> {
  return apiFetch<void>("/meetings/bot", {
    method: "POST",
    body: JSON.stringify({ calendar_event_id: calendarEventId }),
  });
}

export function disableMeetingBot(meetingId: string): Promise<void> {
  return apiFetch<void>(`/meetings/${meetingId}/bot`, { method: "DELETE" });
}

/** "12:00 - 12:30", rendered in the timezone the API bucketed the day by —
 *  not the browser's. A user checking their agenda from a laptop still set to
 *  UTC must read the same times the backend used to split Today from Tomorrow. */
export function formatTimeRange(startsAt: string, endsAt: string, timeZone: string): string {
  const opts: Intl.DateTimeFormatOptions = {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone,
  };
  try {
    const fmt = new Intl.DateTimeFormat("en-GB", opts);
    return `${fmt.format(new Date(startsAt))} - ${fmt.format(new Date(endsAt))}`;
  } catch {
    // An unknown IANA zone throws in Intl; fall back to the browser's.
    const fmt = new Intl.DateTimeFormat("en-GB", { ...opts, timeZone: undefined });
    return `${fmt.format(new Date(startsAt))} - ${fmt.format(new Date(endsAt))}`;
  }
}
```

- [ ] **Step 2: Add the missing icon**

In `src/components/app/icons.tsx`, add alongside the other exports, matching the existing `IconProps` signature and stroke style used by neighbours such as `BellIcon`:

```tsx
export function UsersIcon(p: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" {...p}>
      <path d="M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1" strokeLinecap="round" />
      <circle cx="9" cy="7" r="3" />
      <path d="M22 19v-1a4 4 0 0 0-3-3.87M16 4.13a4 4 0 0 1 0 5.74" strokeLinecap="round" />
    </svg>
  );
}
```

Read the top of the file first and match the exact `IconProps` type and any shared attribute conventions (some codebases set `strokeLinejoin` globally). `EnvelopeIcon`, `DraftsIcon`, `ExternalLinkIcon`, `CheckIcon`, and `ChevronDownIcon` already exist and are reused by later tasks — do not redefine them.

- [ ] **Step 3: Verify it typechecks**

Run: `npx tsc --noEmit`
Expected: no errors. (This checks the whole project, so a pre-existing error elsewhere is not yours — compare against `git stash && npx tsc --noEmit` if unsure.)

- [ ] **Step 4: Commit**

```bash
git add src/lib/dashboard.ts src/components/app/icons.tsx
git commit -m "$(cat <<'EOF'
feat: dashboard summary API client

Times format in the timezone the API bucketed by rather than the
browser's, so a laptop on the wrong clock still reads the same agenda the
backend split into Today and Tomorrow.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: The inbox setup card

**Files:**
- Create: `inboxos-web/src/components/app/InboxSetupCard.tsx`

**Interfaces:**
- Consumes: `DashboardSetup`, `DashboardStats` (Task 6); `Card` from `@/components/ui/Card`; `ProgressRing` from `@/components/app/ProgressRing`; `EnvelopeIcon`, `DraftsIcon` from `@/components/app/icons`.
- Produces: default export `InboxSetupCard({ setup, stats }: { setup: DashboardSetup; stats: DashboardStats })`.

- [ ] **Step 1: Write the component**

Create `src/components/app/InboxSetupCard.tsx`:

```tsx
import Card from "@/components/ui/Card";
import ProgressRing from "@/components/app/ProgressRing";
import { DraftsIcon, EnvelopeIcon } from "@/components/app/icons";
import type { DashboardSetup, DashboardStats } from "@/lib/dashboard";

function StatRow({
  icon,
  value,
  label,
}: {
  icon: React.ReactNode;
  value: number;
  label: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-cream text-ink/50">
        {icon}
      </span>
      <span className="text-xl font-extrabold text-ink">{value}</span>
      <span className="text-sm text-ink/60">{label}</span>
    </div>
  );
}

export default function InboxSetupCard({
  setup,
  stats,
}: {
  setup: DashboardSetup;
  stats: DashboardStats;
}) {
  const ready = setup.state === "ready";

  return (
    <section>
      <h2 className="text-lg font-extrabold tracking-tight text-ink">
        {ready ? "Your inbox is set up" : "Setting up your inbox"}
      </h2>
      <p className="mt-1 text-sm text-ink/50">
        {ready
          ? "InboxOS has categorized your emails and created reply drafts. Head to Gmail to review them."
          : "We're categorizing the mail already in your inbox. This usually takes a few minutes."}
      </p>

      <Card className="mt-4 overflow-hidden">
        <div className="flex flex-col sm:flex-row">
          <div className="flex items-center justify-center bg-cream/60 p-8 sm:w-56">
            <ProgressRing percent={ready ? 100 : 60} label={ready ? "complete" : "working"} />
          </div>
          <div className="flex flex-1 flex-col justify-center gap-4 p-8">
            <StatRow
              icon={<EnvelopeIcon className="h-4 w-4" />}
              value={stats.emails_categorized}
              label="Emails categorized"
            />
            <StatRow
              icon={<DraftsIcon className="h-4 w-4" />}
              value={stats.drafts_created}
              label="Drafts created"
            />
            <a
              href="https://mail.google.com"
              target="_blank"
              rel="noopener noreferrer"
              className="mt-2 inline-flex w-fit items-center gap-2 rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-accent-dark"
            >
              Open Gmail
              <span aria-hidden="true">→</span>
            </a>
          </div>
        </div>
      </Card>
    </section>
  );
}
```

The ring shows a fixed 60% while syncing rather than a fake animation — there is no real progress figure to report, and inventing a moving one is what the old `SetupView` did wrong.

- [ ] **Step 2: Verify it typechecks**

Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add src/components/app/InboxSetupCard.tsx
git commit -m "$(cat <<'EOF'
feat: inbox setup card with real categorization counts

Two states driven by the server's setup flag rather than a localStorage
value and a timer. While syncing the ring holds a fixed value: there is no
real progress figure to report, and animating a fake one is what the old
SetupView did wrong.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: The meetings panel

**Files:**
- Create: `inboxos-web/src/components/app/MeetingRow.tsx`
- Modify (rewrite): `inboxos-web/src/components/app/MeetingsPanel.tsx`

**Interfaces:**
- Consumes: `AgendaItem`, `formatTimeRange`, `enableMeetingBot`, `disableMeetingBot` (Task 6); `Card`; `ExternalLinkIcon`.
- Produces:
  - `MeetingRow({ item, timezone, onToggle }: { item: AgendaItem; timezone: string; onToggle: (item: AgendaItem, next: boolean) => void })`
  - `MeetingsPanel({ meetings, onToggle }: { meetings: DashboardMeetings; onToggle: (item: AgendaItem, next: boolean) => void })`

Both are presentational — the parent page owns the API calls and error handling, so the optimistic state lives in exactly one place.

- [ ] **Step 1: Write the row**

Create `src/components/app/MeetingRow.tsx`:

```tsx
"use client";

import { ExternalLinkIcon } from "@/components/app/icons";
import { formatTimeRange, type AgendaItem } from "@/lib/dashboard";

export default function MeetingRow({
  item,
  timezone,
  onToggle,
}: {
  item: AgendaItem;
  timezone: string;
  onToggle: (item: AgendaItem, next: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-t border-black/5 px-5 py-4 first:border-t-0">
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-ink">
          {item.title ?? "Untitled meeting"}
        </div>
        <div className="mt-0.5 text-xs text-ink/50">
          {formatTimeRange(item.starts_at, item.ends_at, timezone)}
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          disabled={!item.bot_editable}
          onClick={() => onToggle(item, !item.bot_on)}
          aria-pressed={item.bot_on}
          aria-label={`Notetaker for ${item.title ?? "this meeting"}`}
          className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors ${
            item.bot_on
              ? "border-ink/10 bg-cream text-ink"
              : "border-transparent bg-cream text-ink/35"
          } ${item.bot_editable ? "hover:border-ink/25" : "cursor-not-allowed opacity-60"}`}
        >
          {item.bot_on ? "Joining" : "Off"}
        </button>

        {item.meeting_url ? (
          <a
            href={item.meeting_url}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Open meeting link"
            className="text-ink/30 transition-colors hover:text-ink"
          >
            <ExternalLinkIcon className="h-4 w-4" />
          </a>
        ) : null}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Rewrite the panel**

Replace the entire contents of `src/components/app/MeetingsPanel.tsx`:

```tsx
"use client";

import Card from "@/components/ui/Card";
import MeetingRow from "@/components/app/MeetingRow";
import type { AgendaItem, DashboardMeetings } from "@/lib/dashboard";

function Column({
  title,
  items,
  timezone,
  onToggle,
}: {
  title: string;
  items: AgendaItem[];
  timezone: string;
  onToggle: (item: AgendaItem, next: boolean) => void;
}) {
  return (
    <Card className="overflow-hidden">
      <div className="border-b border-black/5 bg-cream/50 px-5 py-3 text-sm font-semibold text-ink">
        {title}
      </div>
      {items.length === 0 ? (
        <div className="px-5 py-10 text-center text-sm text-ink/40">No meetings scheduled</div>
      ) : (
        <div>
          {items.map((item) => (
            <MeetingRow
              key={item.calendar_event_id}
              item={item}
              timezone={timezone}
              onToggle={onToggle}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

export default function MeetingsPanel({
  meetings,
  onToggle,
}: {
  meetings: DashboardMeetings;
  onToggle: (item: AgendaItem, next: boolean) => void;
}) {
  return (
    <div className="grid items-start gap-4 sm:grid-cols-2">
      <Column
        title="Today"
        items={meetings.today}
        timezone={meetings.timezone}
        onToggle={onToggle}
      />
      <Column
        title="Tomorrow"
        items={meetings.tomorrow}
        timezone={meetings.timezone}
        onToggle={onToggle}
      />
    </div>
  );
}
```

- [ ] **Step 3: Verify it typechecks**

Run: `npx tsc --noEmit`

Expected: **one error**, in `src/app/dashboard/page.tsx`, because the old page still renders `<MeetingsPanel />` with no props. That error is expected and is fixed in Task 10. No other errors.

- [ ] **Step 4: Commit**

```bash
git add src/components/app/MeetingRow.tsx src/components/app/MeetingsPanel.tsx
git commit -m "$(cat <<'EOF'
feat: agenda rows with a working notetaker toggle

Both components stay presentational — the page owns the API calls, so
optimistic state lives in one place. The toggle disables itself when the
API reports bot_editable false rather than offering a control that would
409.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: The two static banners

**Files:**
- Create: `inboxos-web/src/components/app/SubscribeBanner.tsx`
- Create: `inboxos-web/src/components/app/InviteTeamBanner.tsx`

**Interfaces:**
- Consumes: `Card`; `UsersIcon` (Task 6).
- Produces: default exports `SubscribeBanner()` (no props) and `InviteTeamBanner()` (no props).

Both are deliberately presentational: no billing or team model exists in the backend. Do not invent endpoints for them.

- [ ] **Step 1: Write the subscribe banner**

Create `src/components/app/SubscribeBanner.tsx`:

```tsx
import Card from "@/components/ui/Card";

export default function SubscribeBanner() {
  return (
    <Card className="flex items-center justify-between gap-4 p-5">
      <div className="flex items-center gap-4">
        <span aria-hidden="true" className="text-2xl">
          🔒
        </span>
        <div>
          <div className="text-sm font-bold text-ink">Subscribe for full access</div>
          <div className="mt-0.5 text-xs text-ink/50">
            Start a subscription to keep your automations running after your trial.
          </div>
        </div>
      </div>
      <a
        href="/#pricing"
        className="shrink-0 text-sm font-semibold text-accent transition-colors hover:text-accent-dark"
      >
        View plans →
      </a>
    </Card>
  );
}
```

- [ ] **Step 2: Write the invite banner**

Create `src/components/app/InviteTeamBanner.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { UsersIcon } from "@/components/app/icons";

const DISMISSED_KEY = "inboxos_invite_banner_dismissed";

export default function InviteTeamBanner() {
  // Starts hidden and reveals after mount: reading localStorage during render
  // would not match the server-rendered HTML and React would complain.
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    setVisible(window.localStorage.getItem(DISMISSED_KEY) !== "1");
  }, []);

  function dismiss() {
    window.localStorage.setItem(DISMISSED_KEY, "1");
    setVisible(false);
  }

  if (!visible) return null;

  return (
    <div className="flex items-center justify-between gap-4 rounded-2xl bg-cream px-5 py-4">
      <div className="min-w-0">
        <div className="flex items-center gap-2 text-sm font-bold text-ink">
          <UsersIcon className="h-4 w-4 text-ink/60" />
          InboxOS gets smarter when your whole team uses it
        </div>
        <div className="mt-1 text-xs text-ink/50">
          Invite your team to share meeting notes, unlock smarter drafts and save time scheduling
          meetings.
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <a
          href="/dashboard/settings"
          className="inline-flex items-center gap-2 rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-dark"
        >
          <UsersIcon className="h-4 w-4" />
          Invite team
        </a>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss"
          className="px-1 text-lg leading-none text-ink/30 transition-colors hover:text-ink"
        >
          ×
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2a: Verify the settings link target**

The Invite team button points at `/dashboard/settings`. Open `src/app/dashboard/settings/page.tsx` and confirm that route exists. If it does not, change the `href` to `/#pricing` rather than leaving a link to a 404.

- [ ] **Step 3: Verify it typechecks**

Run: `npx tsc --noEmit`
Expected: still only the one expected `page.tsx` error from Task 8.

- [ ] **Step 4: Commit**

```bash
git add src/components/app/SubscribeBanner.tsx src/components/app/InviteTeamBanner.tsx
git commit -m "$(cat <<'EOF'
feat: subscribe and invite-team banners

Both presentational by decision: no billing or team model exists in the
backend, so these link out rather than pretending to act. The invite
banner dismisses to localStorage and reveals after mount to keep the
server-rendered markup consistent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: The page rewrite

**Files:**
- Modify (rewrite): `inboxos-web/src/app/dashboard/page.tsx`
- Modify: `inboxos-web/src/lib/auth.ts`
- Modify: `inboxos-web/src/components/app/AskBar.tsx`

**Interfaces:**
- Consumes: everything from Tasks 6-9.
- Produces: the finished page. `isSetupDone` and `setSetupDone` no longer exist.

- [ ] **Step 1: Rewrite the page**

Replace the entire contents of `src/app/dashboard/page.tsx`:

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Topbar from "@/components/app/Topbar";
import Card from "@/components/ui/Card";
import AskBar from "@/components/app/AskBar";
import InboxSetupCard from "@/components/app/InboxSetupCard";
import SubscribeBanner from "@/components/app/SubscribeBanner";
import InviteTeamBanner from "@/components/app/InviteTeamBanner";
import MeetingsPanel from "@/components/app/MeetingsPanel";
import Toast, { type ToastMessage } from "@/components/ui/Toast";
import { ChevronDownIcon, RefreshIcon } from "@/components/app/icons";
import {
  disableMeetingBot,
  enableMeetingBot,
  getDashboardSummary,
  type AgendaItem,
  type DashboardSummary,
} from "@/lib/dashboard";

function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Morning";
  if (h < 18) return "Afternoon";
  return "Evening";
}

function TopbarActions({ onRefresh }: { onRefresh: () => void }) {
  return (
    <>
      <button className="flex items-center gap-1 rounded-full border border-ink/10 bg-cream px-3 py-1.5 text-sm font-medium text-ink/70">
        Personal
        <ChevronDownIcon className="h-4 w-4" />
      </button>
      <button onClick={onRefresh} className="text-ink/40 hover:text-ink" aria-label="Refresh">
        <RefreshIcon className="h-5 w-5" />
      </button>
    </>
  );
}

function Skeleton() {
  return (
    <div className="space-y-10">
      <div className="mx-auto max-w-2xl pt-4">
        <div className="mx-auto h-8 w-2/3 animate-pulse rounded-lg bg-ink/5" />
        <div className="mt-6 h-12 w-full animate-pulse rounded-full bg-ink/5" />
      </div>
      <div className="h-48 animate-pulse rounded-2xl bg-ink/5" />
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="h-40 animate-pulse rounded-2xl bg-ink/5" />
        <div className="h-40 animate-pulse rounded-2xl bg-ink/5" />
      </div>
    </div>
  );
}

export default function DashboardHome() {
  const router = useRouter();
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setData(await getDashboardSummary());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load your dashboard");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** Flip the pill immediately, then reconcile. On failure we re-fetch rather
   *  than hand-rolling an undo: the server may have partly applied the change,
   *  and its answer is the one worth showing. */
  const toggleBot = useCallback(
    async (item: AgendaItem, next: boolean) => {
      setData((prev) => {
        if (!prev) return prev;
        const patch = (list: AgendaItem[]) =>
          list.map((i) =>
            i.calendar_event_id === item.calendar_event_id ? { ...i, bot_on: next } : i,
          );
        return {
          ...prev,
          meetings: {
            ...prev.meetings,
            today: patch(prev.meetings.today),
            tomorrow: patch(prev.meetings.tomorrow),
          },
        };
      });

      try {
        if (next) {
          await enableMeetingBot(item.calendar_event_id);
        } else if (item.meeting_id) {
          await disableMeetingBot(item.meeting_id);
        }
      } catch (e) {
        setToast({
          id: Date.now(),
          text: e instanceof Error ? e.message : "Could not update the notetaker",
          variant: "error",
        });
      }
      await load();
    },
    [load],
  );

  return (
    <>
      <Topbar title="Dashboard">
        <TopbarActions onRefresh={load} />
      </Topbar>

      <div className="p-8">
        {error ? (
          <Card className="mx-auto max-w-md p-8 text-center">
            <div className="text-sm font-semibold text-ink">{error}</div>
            <button
              onClick={load}
              className="mt-4 rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white hover:bg-accent-dark"
            >
              Try again
            </button>
          </Card>
        ) : !data ? (
          <Skeleton />
        ) : (
          <div className="mx-auto max-w-4xl space-y-10">
            <div className="mx-auto max-w-2xl pt-4 text-center">
              <h2 className="mb-6 text-2xl font-extrabold tracking-tight">
                {greeting()}, {data.user.first_name}. Anything you&apos;d like to know?
              </h2>
              <AskBar onSubmit={() => router.push("/dashboard/chat")} />
            </div>

            <InboxSetupCard setup={data.setup} stats={data.stats} />
            <SubscribeBanner />
            <InviteTeamBanner />

            <div>
              <h3 className="mb-3 text-lg font-extrabold tracking-tight text-ink">Your meetings</h3>
              <MeetingsPanel meetings={data.meetings} onToggle={toggleBot} />
            </div>
          </div>
        )}
      </div>

      <Toast toast={toast} onDismiss={() => setToast(null)} />
    </>
  );
}
```

Gone with the rewrite: `SetupView`, `MatureView`, the four-step `setTimeout`, the `AnalyticsCard` trio, the hardcoded scheduling-link card, and the `isSetupDone`/`setSetupDone` imports.

- [ ] **Step 2: Drop the dead setup flag**

In `src/lib/auth.ts`, remove all four of these:

1. The `const SETUP = "inboxos_setup_done";` declaration
2. The `isSetupDone` function
3. The `setSetupDone` function
4. The `clearFlag(SETUP);` line inside `signOut` **and** the one inside `resetOnboarding`

Leave `KEY`, `ONBOARDED`, `INBOX_PREF` and everything touching them alone — the onboarding flow still uses them.

- [ ] **Step 3: Confirm nothing else referenced it**

Run: `grep -rn "isSetupDone\|setSetupDone\|inboxos_setup_done" src/`
Expected: **no output**. Any hit is a caller you have just broken — fix it before continuing.

- [ ] **Step 4: Restyle the ask bar**

In `src/components/app/AskBar.tsx`, widen the form to match the screenshot's proportions. Change the form's `className` from:

```
"flex items-center gap-3 rounded-full border border-ink/10 bg-card px-4 py-3"
```

to:

```
"flex items-center gap-3 rounded-full border border-ink/15 bg-card px-5 py-4 shadow-sm"
```

Change nothing else. The props, the chips, the mic, the busy spinner, and the submit behaviour all stay exactly as they are — no `+` button and no `Balanced` selector, per the spec.

- [ ] **Step 5: Verify it typechecks and builds**

Run: `npx tsc --noEmit && npm run lint && npm run build`
Expected: all three clean. The `page.tsx` error expected since Task 8 is now resolved.

- [ ] **Step 6: Verify the page in a browser**

Start the frontend (`npm run dev`) with the backend stack up, and sign in. On `/dashboard`, confirm:

1. A skeleton appears briefly, then the real page — no flash of the old setup animation.
2. The greeting reads `Morning|Afternoon|Evening, <your first name>.`
3. The setup card's two numbers match `GET /v1/dashboard/summary`.
4. Today/Tomorrow columns list your real calendar events at the right times, or show "No meetings scheduled".
5. Toggling a row to **Off** flips the pill instantly, and it stays Off after a browser reload.
6. Toggling it back to **Joining** works, and also survives a reload.
7. A row whose meeting has already started shows a dimmed, unclickable pill.
8. Dismissing the invite banner hides it, and it stays hidden after reload.

- [ ] **Step 7: Verify the error state**

Stop the backend (`docker compose stop api`) and reload `/dashboard`.

Expected: the error card with a working **Try again** button — not an infinite skeleton and not a blank page. Restart the API and confirm Try again recovers the page.

- [ ] **Step 8: Commit**

```bash
git add src/app/dashboard/page.tsx src/lib/auth.ts src/components/app/AskBar.tsx
git commit -m "$(cat <<'EOF'
feat: rebuild the dashboard home on real API data

One fetch drives the page, with a skeleton and a retry card in place of
the old localStorage flag and its four-step fake setup animation. Bot
toggles apply optimistically and then re-fetch, because on failure the
server's answer is worth more than a hand-rolled undo.

Removes SetupView, MatureView, the placeholder analytics tiles, the
hardcoded scheduling link, and isSetupDone/setSetupDone.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage** — every section maps to a task:

| Spec section | Task |
| --- | --- |
| §1.1 `activity_events` | 1 |
| §1.2 `users.initial_sync_at` | 1 |
| §1.3 Migration | 1 |
| §2 Recording service | 2 |
| §2.1 Call sites (categorization, drafts, sync stamp) | 3 |
| §3 Setup state | 4 |
| §4.1 Calendar-backed agenda | 4 |
| §4.2 Timezone bucketing | 4 |
| §4.3 `bot_on` / `bot_editable` | 4 |
| §5.1 `GET /dashboard/summary` | 4 |
| §5.2 `POST /meetings/bot` | 5 |
| §6.1 `lib/dashboard.ts` | 6 |
| §6.2 Page rewrite + auth cleanup | 10 |
| §6.3 `InboxSetupCard` | 7 |
| §6.3 `MeetingsPanel` / `MeetingRow` | 8 |
| §6.3 Banners | 9 |
| §6.3 AskBar restyle | 10 |
| §7 Verification | Per-task verification steps |

No gaps.

**Type consistency** — checked across task boundaries:

- `record_draft_created(user_id, source_message_id)` is defined in Task 2 and called with `(user_id, e.id)` in Task 3. Consistent.
- `DashboardStats` / `DashboardSetup` / `AgendaItem` / `DashboardMeetings` are defined in Task 4's schema module, returned by Task 4's service, and mirrored field-for-field by Task 6's TypeScript types. Names and nullability match: `meeting_id`, `title`, and `meeting_url` are nullable on both sides; `starts_at` and `ends_at` are non-null on both.
- `MeetingsPanel` takes `{ meetings, onToggle }` in Task 8 and is called with exactly those props in Task 10.
- `onToggle: (item: AgendaItem, next: boolean) => void` is identical in `MeetingRow`, `MeetingsPanel`, and the page's `toggleBot`.
- `BOT_LOCKED_STATUSES` (Task 4) and `LOCKED_STATUSES` (Task 5) hold the same five statuses. The names differ because they live in different modules; both carry a comment pointing at the other, and Task 5 Step 2 states the requirement to keep them in step.

**Placeholder scan:** no TBDs, no "add error handling", no "similar to Task N". Every code step carries the actual code.

One deliberate deviation from the skill's default task shape: steps are **verify** rather than **red-green-refactor**, because the spec rules out writing tests. Each task still ends with a concrete command and a stated expected result.

---

## Notable risks

1. **Task 3 Step 5 is the load-bearing check.** If the count moves on a second `sync_last_7_days` run, the unique constraint is not doing its job and every number on the dashboard is untrustworthy. Do not proceed past it on a "close enough".
2. **`drafts_created` will read 0 or 1 for most users.** There is exactly one `create_draft` call site in the backend (`services/digest/scheduling.py`), reached only when an incoming email asks to book a meeting. This is correct behaviour, not a bug — the screenshot's "3" came from a product with an auto-reply-draft feature this backend does not have yet (`workers/jobs/reply_draft_job.py` is an empty file).
3. **The agenda costs one Composio calendar call per dashboard load.** Accepted in the spec. If the dashboard feels slow, that call is the first thing to measure.
4. **Task 5 does a second calendar read** to resolve the event id. It runs only on toggle-on, not on page load, so it is not on the hot path — but it does mean turning the bot on for an event more than 48 hours out returns 404. `EVENT_LOOKUP_HOURS` is the knob if that proves too tight in practice.
