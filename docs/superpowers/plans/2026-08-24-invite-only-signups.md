# Invite-Only Signups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn payments back on and close public signups, so the first ~100 customers get in by invitation only, each with a 14-day trial and a real paywall behind it.

**Architecture:** Signup and login are one code path (Google OAuth), so the gate is a single question asked in the callback — *is this a signup?* Existing users pass unconditionally; new users need a row in a new `invited_emails` table. Restoring the paywall is un-commenting seven `BILLING DISABLED` sites across two repos, preceded by a migration that backfills a trial for everyone who signed up while billing was off.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, pytest + pytest-asyncio (`asyncio_mode = "auto"`), Next.js App Router + Tailwind (`inboxos-web`).

**Spec:** `docs/superpowers/specs/2026-08-24-invite-only-signups-design.md`

## Global Constraints

- **Two repos.** API is `/Users/abcom/Desktop/openfoundry/InboxPilot`; web is `/Users/abcom/Desktop/openfoundry/inboxos-web` (a sibling repo, not a subdirectory). Tasks state which repo they touch. Commit in each repo separately.
- **Trial length is `14` days.** `settings.TRIAL_DAYS = 14` is the single source of truth for runtime; migrations hard-code their own literal and must never import it.
- **Emails are normalised with `.strip().lower()`** at every write and every read of `invited_emails`. This is the gate's correctness hinge.
- **The gate matches the Google ID-token email** (`profile["email"]`), which Google has verified. `google_sub` remains the identity key for existing users.
- **Existing users always pass the gate.** The invite check applies only on the signup branch.
- **Alembic head before this work: `b3e9d4712a06`.** Two new migrations chain from it in order: `a7c3e1d90f42` (table), then `c8e2f4a10b57` (backfill). Confirm with `uv run alembic heads` before writing each.
- **Run tests with `uv run pytest`** from the InboxPilot root. `pythonpath = ["src"]` is already configured, so imports are `from models.x import Y`, not `from src.models.x import Y`.
- **The `db` fixture requires a reachable Postgres** at `settings.DATABASE_URL`. `make test` runs the suite in Docker Compose; `uv run pytest` needs the local DB up.
- **Ruff line length and formatting**: run `uv run ruff format src tests scripts` and `uv run ruff check src tests` before each commit.

---

### Task 1: Recover the deleted test harness

`pyproject.toml:58` documents a `tests/conftest.py` that does not exist. It and `tests/factories.py` were deleted as collateral in commit `6db459e` ("feat: new meeting page and dashboard re design", 2026-08-04) — 50 lines and 15 lines respectively, unrelated to that commit's subject. Every DB-backed test in this plan needs them back.

Recover, don't rewrite: the originals encode a rollback-isolation decision (a session joining the outer transaction as a SAVEPOINT so service-code `commit()` calls don't end the isolation) that is easy to get subtly wrong.

**Files:**
- Create: `tests/conftest.py` (recovered from `6db459e^`)
- Create: `tests/factories.py` (recovered from `6db459e^`)
- Create: `tests/test_harness.py`

**Interfaces:**
- Consumes: nothing.
- Produces: pytest fixtures `engine` (session-scoped), `db` (`AsyncSession`, rolled back per test), `user` (a persisted `User`); and `make_user(db, **overrides) -> User` from `tests.factories`.

- [ ] **Step 1: Recover both files from git**

```bash
git show 6db459e^:tests/conftest.py > tests/conftest.py
git show 6db459e^:tests/factories.py > tests/factories.py
```

- [ ] **Step 2: Verify the recovered content**

`tests/conftest.py` must contain `join_transaction_mode="create_savepoint"` and a session-scoped `engine` fixture. `tests/factories.py` must define `async def make_user(db, **overrides) -> User`.

```bash
grep -c "create_savepoint" tests/conftest.py   # expect 1
grep -c "async def make_user" tests/factories.py  # expect 1
```

- [ ] **Step 3: Write a test that proves the harness isolates**

Create `tests/test_harness.py`:

```python
"""The recovered harness must actually roll back.

`tests/conftest.py` was deleted by accident in 6db459e and recovered here. Its
whole value is the rollback isolation, and a harness that silently stopped
rolling back would leave the suite passing while filling the development
database with rows. These two tests fail if that happens: the first writes a
user and commits, the second counts users with that email and must find none.
"""

from sqlalchemy import func, select

from models.users import User
from tests.factories import make_user

LEAKED_EMAIL = "harness-isolation-probe@example.com"


async def test_writes_and_commits_are_visible_inside_the_test(db):
    user = await make_user(db, email=LEAKED_EMAIL)
    # A service-code commit must not end the test's isolation — that is what
    # join_transaction_mode="create_savepoint" buys.
    await db.commit()
    found = await db.scalar(select(User).where(User.email == LEAKED_EMAIL))
    assert found is not None
    assert found.id == user.id


async def test_the_previous_test_left_nothing_behind(db):
    count = await db.scalar(
        select(func.count()).select_from(User).where(User.email == LEAKED_EMAIL)
    )
    assert count == 0, "harness stopped rolling back — it is writing to the real database"
```

- [ ] **Step 4: Run it**

Run: `uv run pytest tests/test_harness.py -v`
Expected: 2 passed. If `test_the_previous_test_left_nothing_behind` fails, the isolation is broken — stop and fix the harness before any other task, because every later test will pollute the database.

If it errors with a connection failure, start Postgres (`docker compose up -d db`) and re-run.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/factories.py tests/test_harness.py
git commit -m "test: recover the db harness deleted in 6db459e

tests/conftest.py and tests/factories.py were removed as collateral in an
unrelated commit, leaving pyproject.toml's asyncio_default_fixture_loop_scope
comment pointing at a file that did not exist and no way to write a
db-backed test. Recovered verbatim, plus a test that fails if the rollback
isolation ever stops working."
```

---

### Task 2: The `invited_emails` table

**Files:**
- Create: `src/models/invites.py`
- Modify: `src/models/__init__.py` (add the import)
- Create: `alembic/versions/a7c3e1d90f42_invited_emails.py`
- Create: `tests/test_invited_emails_model.py`

**Interfaces:**
- Consumes: `db` fixture and `make_user` from Task 1.
- Produces: `models.invites.InvitedEmail` with columns `id`, `email`, `note`, `invited_at`, `claimed_at`, `claimed_by_user_id`, `created_at`, `updated_at`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_invited_emails_model.py`:

```python
"""The invite allowlist's storage contract.

Only two things about this table are load-bearing enough to test: the unique
index (two invites for one mailbox is a bug, not a second slot) and that
`claimed_at`/`claimed_by_user_id` start null, since `claim` distinguishes
claimed from unclaimed by exactly that.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models.invites import InvitedEmail
from tests.factories import make_user


async def test_a_new_invite_starts_unclaimed(db):
    db.add(InvitedEmail(email="someone@example.com", note="met at a call"))
    await db.flush()

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == "someone@example.com"))
    assert row.claimed_at is None
    assert row.claimed_by_user_id is None
    assert row.invited_at is not None
    assert row.note == "met at a call"


async def test_the_same_email_cannot_be_invited_twice(db):
    db.add(InvitedEmail(email="dup@example.com"))
    await db.flush()
    db.add(InvitedEmail(email="dup@example.com"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_an_invite_records_who_claimed_it(db):
    user = await make_user(db, email="claimer@example.com")
    row = InvitedEmail(email="claimer@example.com")
    db.add(row)
    await db.flush()

    row.claimed_by_user_id = user.id
    await db.flush()

    assert row.claimed_by_user_id == user.id
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_invited_emails_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.invites'`

- [ ] **Step 3: Write the model**

Create `src/models/invites.py`:

```python
"""The signup allowlist.

Signups are closed while the first ~100 customers are onboarded by hand, and
signup is not a separate code path from login — both are the Google OAuth
callback — so the allowlist is what tells the two apart. A row here is
permission for one mailbox to become a user, once.

`email` is stored lowercased. Google returns the address as the user's provider
spells it, so `Nilesh@X.com` and `nilesh@x.com` are the same mailbox and must
not be two rows; `services.auth.invites.normalize_email` is the only thing that
should ever write this column.

`invited_at` is deliberately not `created_at`. `created_at` is an audit
timestamp that moves if a row is ever recreated or corrected; "when did we
decide to invite this person" is a product fact we want in a funnel query.

`claimed_at` being null is the whole definition of "unclaimed" — `revoke` reads
it to decide whether deleting the row is safe. It is a *record*, not a lock:
existing users always pass the gate, so clearing it would not remove anyone's
access.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin


class InvitedEmail(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "invited_emails"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # SET NULL, not CASCADE: deleting a user must not delete the record that
    # this slot was used. CASCADE would silently free the slot and lose the
    # history of how that person got in.
    claimed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        state = "claimed" if self.claimed_at else "open"
        return f"<InvitedEmail {self.email} {state}>"
```

- [ ] **Step 4: Register the model**

`src/models/__init__.py` — add in alphabetical position, after the `google` import:

```python
from models import invites as invites  # noqa: F401
```

This is not optional. That file's docstring explains why: SQLAlchemy resolves `ForeignKey` targets by name at flush time, so a module nobody imported looks healthy until its first flush.

- [ ] **Step 5: Write the migration**

Create `alembic/versions/a7c3e1d90f42_invited_emails.py`:

```python
"""invited_emails

Revision ID: a7c3e1d90f42
Revises: b3e9d4712a06
Create Date: 2026-08-24

The signup allowlist. Schema only — the backfill of existing accounts is a
separate revision (c8e2f4a10b57) so that this one is a pure structural change
and can be reasoned about on its own.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e1d90f42"
down_revision: str | None = "b3e9d4712a06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invited_emails",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column(
            "invited_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "claimed_by_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Unique, not just indexed: the gate asks "is this mailbox invited" and two
    # rows for one mailbox would mean two answers.
    op.create_index("ix_invited_emails_email", "invited_emails", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_invited_emails_email", table_name="invited_emails")
    op.drop_table("invited_emails")
```

- [ ] **Step 6: Apply the migration and run the tests**

```bash
uv run alembic heads          # expect: a7c3e1d90f42 (head)
uv run alembic upgrade head
uv run pytest tests/test_invited_emails_model.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Verify the downgrade works**

A migration whose downgrade is broken is discovered at the worst possible moment.

```bash
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: both succeed silently.

- [ ] **Step 8: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/models/invites.py src/models/__init__.py alembic/versions/a7c3e1d90f42_invited_emails.py tests/test_invited_emails_model.py
git commit -m "feat: add invited_emails allowlist table"
```

---

### Task 3: The `invites` service

**Files:**
- Create: `src/services/auth/invites.py`
- Create: `tests/test_invites_service.py`

**Interfaces:**
- Consumes: `models.invites.InvitedEmail` (Task 2).
- Produces:
  - `normalize_email(raw: str) -> str`
  - `async is_invited(db: AsyncSession, email: str) -> bool`
  - `async claim(db: AsyncSession, email: str, user_id: uuid.UUID) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_invites_service.py`:

```python
"""The allowlist's read/write rules.

Case-insensitivity is the interesting property here. Google hands us the address
as the user's provider spells it, so every one of these has to agree on what
"the same mailbox" means or an invited customer gets turned away at the door.
"""

from sqlalchemy import select

from models.invites import InvitedEmail
from services.auth import invites
from tests.factories import make_user


def test_normalize_lowercases_and_strips():
    assert invites.normalize_email("  Nilesh@X.COM ") == "nilesh@x.com"


def test_normalize_is_idempotent():
    once = invites.normalize_email(" A@B.com ")
    assert invites.normalize_email(once) == once


async def test_uninvited_email_is_not_invited(db):
    assert await invites.is_invited(db, "nobody@example.com") is False


async def test_invited_email_is_invited(db):
    db.add(InvitedEmail(email="yes@example.com"))
    await db.flush()
    assert await invites.is_invited(db, "yes@example.com") is True


async def test_is_invited_ignores_case_and_whitespace(db):
    db.add(InvitedEmail(email="mixed@example.com"))
    await db.flush()
    assert await invites.is_invited(db, "  Mixed@Example.COM ") is True


async def test_an_already_claimed_invite_still_counts_as_invited(db):
    """The claim is a record, not a lock.

    A user who is deleted and signs up again, or a second Google account on the
    same mailbox, must not be locked out by their own past claim — and the gate
    only consults this on the signup branch anyway.
    """
    user = await make_user(db)
    row = InvitedEmail(email="repeat@example.com")
    db.add(row)
    await db.flush()
    await invites.claim(db, "repeat@example.com", user.id)

    assert await invites.is_invited(db, "repeat@example.com") is True


async def test_claim_records_who_and_when(db):
    user = await make_user(db)
    db.add(InvitedEmail(email="claimme@example.com"))
    await db.flush()

    await invites.claim(db, "CLAIMME@example.com", user.id)

    row = await db.scalar(
        select(InvitedEmail).where(InvitedEmail.email == "claimme@example.com")
    )
    assert row.claimed_by_user_id == user.id
    assert row.claimed_at is not None


async def test_claim_twice_keeps_the_first_claimer(db):
    first = await make_user(db)
    second = await make_user(db)
    db.add(InvitedEmail(email="once@example.com"))
    await db.flush()

    await invites.claim(db, "once@example.com", first.id)
    await invites.claim(db, "once@example.com", second.id)

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == "once@example.com"))
    assert row.claimed_by_user_id == first.id


async def test_claiming_an_email_with_no_invite_is_a_no_op(db):
    """Belt and braces: `claim` runs after the gate said yes, but it must not
    explode if it is ever called for an address with no row."""
    user = await make_user(db)
    await invites.claim(db, "ghost@example.com", user.id)  # must not raise
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_invites_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'invites' from 'services.auth'`

- [ ] **Step 3: Write the service**

Create `src/services/auth/invites.py`:

```python
"""Is this mailbox allowed to become a user?

Signups are closed while the first ~100 customers are onboarded by hand. Because
signup and login are the same Google OAuth callback, this module is what tells
them apart: `is_invited` is consulted only when the callback is about to create
a new user, and existing users are never asked.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.invites import InvitedEmail


def normalize_email(raw: str) -> str:
    """The one spelling of an address that `invited_emails.email` stores.

    Every write and every lookup goes through here. Google returns the address
    as the user's provider spells it, so an invite added as `Nilesh@X.com` must
    still match a login that arrives as `nilesh@x.com`.
    """
    return raw.strip().lower()


async def is_invited(db: AsyncSession, email: str) -> bool:
    """Whether this mailbox has a slot.

    Claimed invites still return True. The claim is a record of a slot being
    used, not a lock — and this is only ever asked on the signup branch, so a
    claimed row answering True cannot let an extra person in past the one the
    row was for.
    """
    row = await db.scalar(
        select(InvitedEmail.id).where(InvitedEmail.email == normalize_email(email))
    )
    return row is not None


async def claim(db: AsyncSession, email: str, user_id: uuid.UUID) -> None:
    """Record that `user_id` used this invite. Idempotent.

    Only the first claim is kept: re-running must not rewrite who took a slot,
    which is the one fact this row exists to preserve. An address with no row is
    a silent no-op — the gate has already run by the time we get here, so
    raising would turn a bookkeeping miss into a failed login.
    """
    row = await db.scalar(
        select(InvitedEmail).where(InvitedEmail.email == normalize_email(email))
    )
    if row is None or row.claimed_at is not None:
        return

    row.claimed_at = datetime.now(timezone.utc)
    row.claimed_by_user_id = user_id
    await db.flush()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_invites_service.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/services/auth/invites.py tests/test_invites_service.py
git commit -m "feat: add invite allowlist lookup and claim"
```

---

### Task 4: Refuse uninvited signups in `upsert_user_from_google`

**Files:**
- Modify: `src/core/exceptions.py` (add `SignupNotInvited`)
- Modify: `src/services/auth/oauth.py:13-36` (`upsert_user_from_google`)
- Create: `tests/test_signup_gate.py`

**Interfaces:**
- Consumes: `normalize_email` (Task 3) — only indirectly, via the caller.
- Produces:
  - `core.exceptions.SignupNotInvited` (subclass of `AppError`, 403)
  - `upsert_user_from_google(db, profile, *, signup_allowed: bool) -> tuple[User, bool]`

**Note the signature change is breaking on purpose.** `signup_allowed` is keyword-only and has no default, so any existing caller fails to import-time-check rather than silently defaulting to open signups. `google_callback` is the only caller; Task 5 updates it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_signup_gate.py`:

```python
"""The gate itself: may this Google identity become a user?

`upsert_user_from_google` is where signup and login diverge, so it is where the
refusal belongs. Two properties matter and are easy to lose in a refactor: an
*existing* user is never gated, and a refused signup writes nothing.
"""

import pytest
from sqlalchemy import func, select

from core.exceptions import SignupNotInvited
from models.users import User
from services.auth.oauth import upsert_user_from_google
from tests.factories import make_user

PROFILE = {
    "sub": "google-sub-newcomer",
    "email": "newcomer@example.com",
    "name": "New Comer",
    "picture": "https://example.com/p.png",
}


async def test_uninvited_signup_is_refused(db):
    with pytest.raises(SignupNotInvited):
        await upsert_user_from_google(db, PROFILE, signup_allowed=False)


async def test_a_refused_signup_writes_no_user(db):
    """The refusal must land before `db.add`, or a rejected visitor still exists."""
    before = await db.scalar(select(func.count()).select_from(User))
    with pytest.raises(SignupNotInvited):
        await upsert_user_from_google(db, PROFILE, signup_allowed=False)
    after = await db.scalar(select(func.count()).select_from(User))
    assert after == before


async def test_allowed_signup_creates_the_user_and_reports_created(db):
    user, created = await upsert_user_from_google(db, PROFILE, signup_allowed=True)
    assert created is True
    assert user.email == "newcomer@example.com"
    assert user.google_sub == "google-sub-newcomer"
    assert user.last_login_at is not None


async def test_an_existing_user_logs_in_even_when_signups_are_closed(db):
    """The whole point: login keeps working while the front door is shut."""
    existing = await make_user(db, email="already@example.com", google_sub="google-sub-existing")

    user, created = await upsert_user_from_google(
        db,
        {"sub": "google-sub-existing", "email": "already@example.com", "name": "Already In"},
        signup_allowed=False,
    )

    assert created is False
    assert user.id == existing.id


async def test_an_existing_user_has_their_profile_refreshed(db):
    await make_user(db, email="old@example.com", google_sub="google-sub-refresh")

    user, created = await upsert_user_from_google(
        db,
        {"sub": "google-sub-refresh", "email": "new@example.com", "name": "Renamed"},
        signup_allowed=False,
    )

    assert created is False
    assert user.email == "new@example.com"
    assert user.full_name == "Renamed"


async def test_the_exception_carries_the_email_for_logs(db):
    with pytest.raises(SignupNotInvited) as caught:
        await upsert_user_from_google(db, PROFILE, signup_allowed=False)
    assert "newcomer@example.com" in str(caught.value)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_signup_gate.py -v`
Expected: FAIL — `ImportError: cannot import name 'SignupNotInvited' from 'core.exceptions'`

- [ ] **Step 3: Add the exception**

`src/core/exceptions.py` — after `UnauthorizedError`:

```python
class SignupNotInvited(AppError):
    """A new Google identity with no invite.

    403 rather than 401: the caller authenticated fine with Google, they are
    just not allowed to create an account. The OAuth callback catches this and
    redirects instead of letting the handler render it, so the status code
    matters only if some future caller lets it escape.
    """

    status_code = status.HTTP_403_FORBIDDEN
    detail = "Signups are invite-only"

    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(f"{self.detail}: {email}")
```

- [ ] **Step 4: Add the gate**

Replace `upsert_user_from_google` in `src/services/auth/oauth.py` (currently lines 13-36):

```python
async def upsert_user_from_google(
    db: AsyncSession, profile: dict, *, signup_allowed: bool
) -> tuple[User, bool]:
    """Find or create the user behind a Google profile. Returns (user, created).

    `signup_allowed` gates the create branch only — an existing user logs in
    regardless, which is what keeps the product working for current customers
    while the front door is shut. It is keyword-only and has no default so that
    a new caller cannot open signups by forgetting it.

    The refusal is an exception rather than a `None` return because a caller who
    ignores it would silently reopen public signups. It is raised before
    `db.add`, so a refused signup leaves nothing behind.
    """
    user = await db.scalar(select(User).where(User.google_sub == profile["sub"]))
    now = datetime.now(timezone.utc)
    created = user is None

    if user is None:  # ← signup
        if not signup_allowed:
            raise SignupNotInvited(profile["email"])
        user = User(
            google_sub=profile["sub"],
            email=profile["email"],
            full_name=profile.get("name"),
            picture=profile.get("picture"),
            last_login_at=now,
        )
        db.add(user)

    else:
        user.email = profile["email"]
        user.full_name = profile.get("name")
        user.picture = profile.get("picture")
        user.last_login_at = now

    await db.commit()
    await db.refresh(user)

    return user, created
```

Add the import at the top of `src/services/auth/oauth.py`:

```python
from core.exceptions import SignupNotInvited
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_signup_gate.py -v`
Expected: 6 passed.

- [ ] **Step 6: Confirm nothing else called the old signature**

```bash
grep -rn "upsert_user_from_google" src/ scripts/ tests/
```

Expected: the definition, `src/api/v1/auth.py` (fixed in Task 5), and the new test. If anything else appears, it needs the keyword too.

- [ ] **Step 7: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/core/exceptions.py src/services/auth/oauth.py tests/test_signup_gate.py
git commit -m "feat: refuse uninvited signups in the google upsert"
```

---

### Task 5: Wire the gate into the OAuth callback

**Files:**
- Modify: `src/core/config.py:35` (add `LOGIN_URL` beside `POST_LOGIN_REDIRECT_URL`)
- Modify: `src/api/v1/auth.py:70-101` (`google_callback`)
- Modify: `.env.example:21`
- Modify: `render.yaml:96`
- Modify: `infra/locals.tf:29`, `infra/ecs.tf:47`
- Create: `tests/test_oauth_callback_gate.py`

**Interfaces:**
- Consumes: `invites.is_invited` (Task 3), `upsert_user_from_google(..., signup_allowed=) -> (user, created)` and `SignupNotInvited` (Task 4).
- Produces: `settings.LOGIN_URL`; the callback's `?error=not_invited` redirect contract that Task 11's web change reads.

- [ ] **Step 1: Write the failing test**

Create `tests/test_oauth_callback_gate.py`:

```python
"""The callback's behaviour on a refused signup.

The Google round-trip is patched out — what is under test is the callback's own
branching, not Google. Two things are easy to get wrong and both are pinned
here: the redirect target, and clearing the PKCE cookies on the way out. A
refused visitor who keeps a live 10-minute `oauth_state` carries it into their
next attempt.
"""

import pytest
from fastapi import status

from api.v1 import auth as auth_mod
from core.config import settings
from models.invites import InvitedEmail
from tests.factories import make_user

NEWCOMER = {
    "sub": "google-sub-callback-new",
    "email": "callback-new@example.com",
    "name": "Call Back",
}


@pytest.fixture
def google_returns(monkeypatch):
    """Make `exchange_code_for_profile` hand back a chosen profile."""

    def _set(profile):
        async def _fake(code, verifier):
            return profile

        monkeypatch.setattr(auth_mod.service, "exchange_code_for_profile", _fake)

    return _set


async def _call(db, request):
    return await auth_mod.google_callback(request, db, code="c", state="s")


class _Request:
    """Minimal stand-in carrying just the cookies the callback reads."""

    def __init__(self, state="s", verifier="v"):
        self.cookies = {"oauth_state": state, "oauth_verifier": verifier}


async def test_uninvited_signup_redirects_to_login_with_the_error(db, google_returns):
    google_returns(NEWCOMER)
    resp = await _call(db, _Request())

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == f"{settings.LOGIN_URL}?error=not_invited"


async def test_a_refused_signup_clears_the_pkce_cookies(db, google_returns):
    google_returns(NEWCOMER)
    resp = await _call(db, _Request())

    cleared = "".join(resp.headers.getlist("set-cookie"))
    assert "oauth_state=" in cleared
    assert "oauth_verifier=" in cleared


async def test_an_invited_signup_is_let_in_and_the_invite_is_claimed(db, google_returns):
    db.add(InvitedEmail(email=NEWCOMER["email"]))
    await db.flush()
    google_returns(NEWCOMER)

    resp = await _call(db, _Request())

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == settings.POST_LOGIN_REDIRECT_URL

    from sqlalchemy import select

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == NEWCOMER["email"]))
    assert row.claimed_at is not None


async def test_an_existing_user_is_let_in_without_an_invite(db, google_returns):
    await make_user(db, email="cb-existing@example.com", google_sub="google-sub-cb-existing")
    google_returns(
        {"sub": "google-sub-cb-existing", "email": "cb-existing@example.com", "name": "X"}
    )

    resp = await _call(db, _Request())

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == settings.POST_LOGIN_REDIRECT_URL
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_oauth_callback_gate.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'LOGIN_URL'`

- [ ] **Step 3: Add the setting**

`src/core/config.py` — immediately after `POST_LOGIN_REDIRECT_URL` on line 35:

```python
    # Where to send a browser we are turning away (an uninvited signup). Set
    # explicitly rather than derived from POST_LOGIN_REDIRECT_URL by string
    # surgery, which would break the moment that value's path changes.
    LOGIN_URL: str = "http://localhost:3000/login"
```

- [ ] **Step 4: Wire the callback**

In `src/api/v1/auth.py`, add to the imports:

```python
from core.exceptions import SignupNotInvited
from services.auth import invites, oauth, service
```

(the existing line is `from services.auth import oauth, service`)

Then replace the two lines in `google_callback` that currently read:

```python
    user = await oauth.upsert_user_from_google(db, profile)
    access, refresh = await oauth.issue_tokens(db, user)
```

with:

```python
    # Asked unconditionally, and ignored on the login branch, so the callback
    # reads as one linear flow instead of testing for the user twice. One extra
    # SELECT per login.
    signup_allowed = await invites.is_invited(db, profile["email"])
    try:
        user, created = await oauth.upsert_user_from_google(
            db, profile, signup_allowed=signup_allowed
        )
    except SignupNotInvited:
        # Same cookie cleanup as the success path: a turned-away visitor must
        # not carry a live oauth_state into their next attempt.
        refused = RedirectResponse(
            f"{settings.LOGIN_URL}?error=not_invited", status_code=status.HTTP_303_SEE_OTHER
        )
        refused.delete_cookie(STATE_COOKIE, path="/")
        refused.delete_cookie(VERIFIER_COOKIE, path="/")
        return refused

    if created:
        await invites.claim(db, profile["email"], user.id)

    access, refresh = await oauth.issue_tokens(db, user)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_oauth_callback_gate.py -v`
Expected: 4 passed.

- [ ] **Step 6: Add the setting to every environment**

`.env.example` — after line 21:

```
# Where to send the browser when a signup is refused (invite-only)
LOGIN_URL=http://localhost:3000/login
```

`render.yaml` — after the `POST_LOGIN_REDIRECT_URL` block at line 96:

```yaml
      - key: LOGIN_URL
        sync: false
```

`infra/locals.tf` — after line 29, deriving from the origin that is already there:

```terraform
  login_url = "${var.frontend_origin}/login"
```

`infra/ecs.tf` — after line 47:

```terraform
    { name = "LOGIN_URL", value = local.login_url },
```

- [ ] **Step 7: Verify the config is wired everywhere**

```bash
grep -rn "LOGIN_URL" src/core/config.py .env.example render.yaml infra/
```

Expected: five hits — config, .env.example, render.yaml, locals.tf, ecs.tf. A missing one means a refused signup redirects to localhost in that environment.

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest -v`
Expected: everything passes. The two `BILLING DISABLED` skips are still skipped — that is Task 8.

- [ ] **Step 9: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/core/config.py src/api/v1/auth.py .env.example render.yaml infra/locals.tf infra/ecs.tf tests/test_oauth_callback_gate.py
git commit -m "feat: gate signups on the invite allowlist in the oauth callback"
```

---

### Task 6: The `invite.py` admin CLI

**Files:**
- Create: `scripts/invite.py`

**Interfaces:**
- Consumes: `models.invites.InvitedEmail`, `services.auth.invites.normalize_email`, and `core.database.run_async` / `with_worker_session` (the pattern in `scripts/drafts_status.py`).
- Produces: nothing other tasks depend on.

No test file: this matches the repo's convention that `scripts/` are operator tools verified by running them, and its logic is `normalize_email` (tested in Task 3) plus two queries. Step 4 exercises every subcommand for real.

- [ ] **Step 1: Write the script**

Create `scripts/invite.py`:

```python
"""Manage the signup allowlist.

Signups are invite-only while the first ~100 customers are onboarded by hand.
This is the whole admin surface for that list.

    uv run python scripts/invite.py add someone@example.com --note "call 2026-08-24"
    uv run python scripts/invite.py list
    uv run python scripts/invite.py list --unclaimed
    uv run python scripts/invite.py revoke someone@example.com

`list` also prints the claimed/total count, so "how many of the 100 are gone" is
one command.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from core.database import run_async, with_worker_session  # noqa: E402
from models.invites import InvitedEmail  # noqa: E402
from models.users import User  # noqa: E402
from services.auth.invites import normalize_email  # noqa: E402


async def _add(db, email: str, note: str | None) -> str:
    normalized = normalize_email(email)
    existing = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == normalized))
    if existing is not None:
        state = "already claimed" if existing.claimed_at else "still open"
        return f"{normalized} was already invited ({state}) — nothing changed."

    db.add(InvitedEmail(email=normalized, note=note))
    await db.commit()
    return f"Invited {normalized}."


async def _list(db, only: str | None) -> list[str]:
    stmt = select(InvitedEmail).order_by(InvitedEmail.invited_at)
    if only == "claimed":
        stmt = stmt.where(InvitedEmail.claimed_at.is_not(None))
    elif only == "unclaimed":
        stmt = stmt.where(InvitedEmail.claimed_at.is_(None))

    rows = list(await db.scalars(stmt))
    if not rows:
        return ["No invites match."]

    out = []
    claimed = 0
    for row in rows:
        who = ""
        if row.claimed_by_user_id:
            user = await db.get(User, row.claimed_by_user_id)
            who = f" by {user.email}" if user else " by (deleted user)"
        if row.claimed_at:
            claimed += 1
            state = f"claimed {row.claimed_at:%Y-%m-%d}{who}"
        else:
            state = "open"
        note = f"  [{row.note}]" if row.note else ""
        out.append(f"  {row.email:<40} {state}{note}")

    # Only meaningful for the unfiltered listing; with a filter the denominator
    # is the filtered set, which would be a misleading "x of 100".
    if only is None:
        out.append("")
        out.append(f"{claimed} claimed / {len(rows)} invited")
    return out


async def _revoke(db, email: str) -> str:
    normalized = normalize_email(email)
    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == normalized))
    if row is None:
        return f"{normalized} is not on the list."
    if row.claimed_at is not None:
        # Deleting this row would not remove their access — existing users
        # always pass the gate — it would only destroy the record of how they
        # got in. Real revocation needs User.is_active to become load-bearing.
        return (
            f"{normalized} already claimed their invite. Deleting the row would not\n"
            f"revoke their access (existing users always pass the signup gate), only\n"
            f"lose the record. Refusing."
        )

    await db.delete(row)
    await db.commit()
    return f"Revoked {normalized}."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="invite an email address")
    p_add.add_argument("email")
    p_add.add_argument("--note", default=None, help="who they are / where they came from")

    p_list = sub.add_parser("list", help="show the allowlist")
    group = p_list.add_mutually_exclusive_group()
    group.add_argument("--claimed", action="store_true")
    group.add_argument("--unclaimed", action="store_true")

    p_revoke = sub.add_parser("revoke", help="remove an unclaimed invite")
    p_revoke.add_argument("email")

    args = parser.parse_args()

    if args.command == "add":
        print(run_async(with_worker_session(lambda db: _add(db, args.email, args.note))))
    elif args.command == "list":
        only = "claimed" if args.claimed else "unclaimed" if args.unclaimed else None
        for line in run_async(with_worker_session(lambda db: _list(db, only))):
            print(line)
    elif args.command == "revoke":
        print(run_async(with_worker_session(lambda db: _revoke(db, args.email))))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify `with_worker_session` accepts a lambda**

The script passes a one-argument callable. Confirm that matches how `core.database.with_worker_session` is used:

```bash
grep -n "def with_worker_session" -A 15 src/core/database.py
```

If it expects a bare coroutine function rather than any callable, adapt the three call sites to `functools.partial(_add, email=..., note=...)` instead of lambdas. Do not change `core/database.py`.

- [ ] **Step 3: Check it parses**

Run: `uv run python scripts/invite.py --help`
Expected: usage text listing `add`, `list`, `revoke`.

- [ ] **Step 4: Exercise every subcommand against the database**

```bash
uv run python scripts/invite.py add "  Probe@Example.COM " --note "cli smoke test"
uv run python scripts/invite.py list
uv run python scripts/invite.py list --unclaimed
uv run python scripts/invite.py add probe@example.com
uv run python scripts/invite.py revoke probe@example.com
uv run python scripts/invite.py list
```

Expected, in order: `Invited probe@example.com.` (normalised — note the input had mixed case and spaces); a listing showing it `open` with its note and `0 claimed / 1 invited`; the same row under `--unclaimed`; `probe@example.com was already invited (still open) — nothing changed.`; `Revoked probe@example.com.`; and `No invites match.`

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts && uv run ruff check scripts
git add scripts/invite.py
git commit -m "feat: add invite.py for managing the signup allowlist"
```

---

### Task 7: Trial length 7 → 14, and backfill the billing-off window

This task must land **before** Task 8. Its migration gives every existing account a subscription row; Task 8 is what makes the absence of one lock them out. In that order the paywall switches on with everybody already holding a trial.

**Files:**
- Modify: `src/core/config.py:174` (`TRIAL_DAYS`)
- Modify: `alembic/versions/f1a2b3c4d5e6_add_billing_tables.py:99` (freeze the literal)
- Create: `alembic/versions/c8e2f4a10b57_backfill_trials_after_billing_off.py`
- Create: `tests/test_trial_backfill.py`

**Interfaces:**
- Consumes: `invited_emails` (Task 2).
- Produces: nothing other tasks depend on in code.

- [ ] **Step 1: Freeze the literal in the old migration**

`alembic/versions/f1a2b3c4d5e6_add_billing_tables.py` interpolates the *live* setting into its SQL at runtime (line 99: `""".replace(":days", str(TRIAL_DAYS))`). Bumping `TRIAL_DAYS` would retroactively change what that 2026-08-02 migration does on any database built from scratch. Migrations are historical records.

Replace `":days"` in the SQL string with the literal `7`, delete the `.replace(...)` call, and remove the now-unused `TRIAL_DAYS` import from that file. The line becomes:

```python
                   now() + interval '7 days', false, false, now(), now()
```

…with the string closing as a plain `"""` and no `.replace()`. Add above the `op.execute`:

```python
    # The 7 is frozen deliberately. This migration originally read
    # settings.TRIAL_DAYS at runtime, which meant changing that setting
    # silently rewrote what this historical migration does on a fresh
    # database. A migration is a record of what happened, not a function of
    # today's config.
```

- [ ] **Step 2: Confirm the old migration no longer imports the setting**

```bash
grep -n "TRIAL_DAYS" alembic/versions/f1a2b3c4d5e6_add_billing_tables.py
```

Expected: no output.

- [ ] **Step 3: Bump the setting**

`src/core/config.py:174`: `TRIAL_DAYS: int = 7` → `TRIAL_DAYS: int = 14`.

- [ ] **Step 4: Write the failing test**

Create `tests/test_trial_backfill.py`:

```python
"""Nobody gets locked out by the paywall coming back on.

Accounts created while billing was off have no `subscriptions` row at all —
`get_or_create_subscription` only runs from `start_checkout`, and a plain GET
never creates one. The moment `resolve_access` stops returning "entitled" every
one of them is locked *and* bounced to the plan picker, which is what the
backfill migration exists to prevent.

These tests exercise the same SQL the migration runs, against the test session,
rather than driving alembic — the assertion worth making is about the rules
(one row per user, never restart a running trial), not about alembic working.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from models.billing import STATUS_AUTHENTICATED, Subscription
from tests.factories import make_user

BACKFILL_SQL = text(
    """
    INSERT INTO subscriptions
        (id, user_id, plan_id, interval, currency, status, trial_ends_at,
         trial_consumed, cancel_at_period_end, comped, created_at, updated_at)
    SELECT gen_random_uuid(), u.id, 'pro', 'monthly', 'USD', 'authenticated',
           now() + interval '14 days', true, false, false, now(), now()
    FROM users u
    ON CONFLICT (user_id) DO NOTHING
    """
)


async def test_a_user_with_no_subscription_gets_a_trial(db):
    user = await make_user(db)
    await db.execute(BACKFILL_SQL)

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub is not None
    assert sub.status == STATUS_AUTHENTICATED
    assert sub.plan_id == "pro"
    assert sub.comped is False
    # trial_consumed, or the next checkout hands them a second free fortnight.
    assert sub.trial_consumed is True
    remaining = sub.trial_ends_at - datetime.now(timezone.utc)
    assert timedelta(days=13) < remaining <= timedelta(days=14)


async def test_an_existing_subscription_is_left_alone(db):
    """ON CONFLICT DO NOTHING: never restart a trial already counting down."""
    user = await make_user(db)
    original = datetime.now(timezone.utc) + timedelta(days=2)
    db.add(
        Subscription(
            user_id=user.id,
            status=STATUS_AUTHENTICATED,
            trial_ends_at=original,
            trial_consumed=True,
        )
    )
    await db.flush()

    await db.execute(BACKFILL_SQL)

    subs = list(await db.scalars(select(Subscription).where(Subscription.user_id == user.id)))
    assert len(subs) == 1
    assert subs[0].trial_ends_at == original


async def test_running_the_backfill_twice_changes_nothing(db):
    user = await make_user(db)
    await db.execute(BACKFILL_SQL)
    first = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    ends_at = first.trial_ends_at

    await db.execute(BACKFILL_SQL)

    subs = list(await db.scalars(select(Subscription).where(Subscription.user_id == user.id)))
    assert len(subs) == 1
    assert subs[0].trial_ends_at == ends_at
```

- [ ] **Step 5: Run it to verify it fails**

Run: `uv run pytest tests/test_trial_backfill.py -v`
Expected: FAIL. The SQL runs, but `test_a_user_with_no_subscription_gets_a_trial` fails its 13–14 day window because `f1a2b3c4d5e6` already granted a 7-day trial to users created before it — or passes trivially if your local `users` table is empty. **Seed a user first** if all three pass immediately: the tests must be shown to exercise real rows.

- [ ] **Step 6: Write the migration**

Create `alembic/versions/c8e2f4a10b57_backfill_trials_after_billing_off.py`:

```python
"""backfill trials for accounts created while billing was off

Revision ID: c8e2f4a10b57
Revises: a7c3e1d90f42
Create Date: 2026-08-24

Billing was switched off on 2026-08-18 and is being switched back on. Accounts
created in between have no `subscriptions` row at all —
`get_or_create_subscription` only runs from `start_checkout`, and a plain GET
never creates one — so restoring the paywall would lock every one of them out
and bounce them to the plan picker.

Mirrors f1a2b3c4d5e6's original backfill. `status = 'authenticated'` is
Razorpay's "mandate signed, first charge not yet due" — the state the access
rules read as trialing — even though no Razorpay subscription exists.

`trial_consumed = true` because this row's creation IS the trial grant. Without
it the next checkout reads the column's default and hands these users a second
full-length trial, which is the exact bug d4e5f6a7b8c9 added the column to
close.

`ON CONFLICT DO NOTHING` makes a re-run harmless and, more importantly, leaves
anyone who did reach checkout with the trial already counting down rather than
restarting it.

The 14 is a hard-coded literal, not settings.TRIAL_DAYS. A migration is a
record of what happened; reading live config is how f1a2b3c4d5e6 ended up
changing its own history when the setting moved.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2f4a10b57"
down_revision: str | None = "a7c3e1d90f42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO subscriptions
                (id, user_id, plan_id, interval, currency, status, trial_ends_at,
                 trial_consumed, cancel_at_period_end, comped, created_at, updated_at)
            SELECT gen_random_uuid(), u.id, 'pro', 'monthly', 'USD', 'authenticated',
                   now() + interval '14 days', true, false, false, now(), now()
            FROM users u
            ON CONFLICT (user_id) DO NOTHING
            """
        )
    )

    # Everyone already here got in before the door was shut, so they hold a
    # slot whether or not anyone typed their address into invite.py. Recording
    # them as claimed keeps `invite.py list`'s "x claimed / y invited" honest
    # and makes them count against the first 100.
    op.execute(
        sa.text(
            """
            INSERT INTO invited_emails
                (id, email, note, invited_at, claimed_at, claimed_by_user_id,
                 created_at, updated_at)
            SELECT gen_random_uuid(), lower(trim(u.email)),
                   'backfilled: signed up before invites existed',
                   now(), now(), u.id, now(), now()
            FROM users u
            ON CONFLICT (email) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    # Deliberately not reversible. Deleting the backfilled rows would have to
    # guess which subscriptions this migration created versus which a user
    # authorised themselves, and getting that wrong destroys live billing
    # state. Downgrading past this point is a restore-from-backup operation.
    pass
```

- [ ] **Step 7: Apply it and run the tests**

```bash
uv run alembic heads          # expect: c8e2f4a10b57 (head)
uv run alembic upgrade head
uv run pytest tests/test_trial_backfill.py -v
```

Expected: 3 passed.

- [ ] **Step 8: Verify the real backfill landed**

```bash
uv run python scripts/invite.py list
```

Expected: every pre-existing user listed as claimed with the `backfilled:` note, and a `n claimed / n invited` line.

- [ ] **Step 9: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/core/config.py alembic/versions/f1a2b3c4d5e6_add_billing_tables.py alembic/versions/c8e2f4a10b57_backfill_trials_after_billing_off.py tests/test_trial_backfill.py
git commit -m "feat: 14-day trials, and backfill the billing-off window

Also freezes f1a2b3c4d5e6's trial literal at 7. It interpolated
settings.TRIAL_DAYS into its SQL at runtime, so bumping the setting would
have retroactively changed what that migration does on a fresh database."
```

---

### Task 8: Restore the paywall (API)

**Files:**
- Modify: `src/services/billing/access.py` (`resolve_access`, `effective_plan_id`)
- Modify: `src/api/v1/billing.py:150` (`_subscription_started`)
- Modify: `src/api/v1/webhooks.py:173` (signature check)
- Modify: `tests/test_mail_access.py:23-27` (remove the skip)
- Modify: `tests/test_mail_sync_trigger.py:66` (remove the skip)
- Delete: `tests/test_billing_disabled_gate.py`

**Interfaces:**
- Consumes: the backfill from Task 7.
- Produces: nothing new — this restores existing behaviour.

Every change here is deleting an early return and un-commenting the body beneath it. Do not rewrite the commented code — un-comment it verbatim. It is the reviewed original.

- [ ] **Step 1: Un-skip the tests first, and watch them fail**

This is the step that proves the restore is real. Remove `pytestmark = pytest.mark.skip(...)` and its `BILLING DISABLED` comment block from `tests/test_mail_access.py:23-27`, and the `@pytest.mark.skip(...)` decorator from `tests/test_mail_sync_trigger.py:66`. Remove the now-unused `import pytest` from `test_mail_access.py` only if nothing else in that file uses it.

Run: `uv run pytest tests/test_mail_access.py tests/test_mail_sync_trigger.py -v`
Expected: **FAIL.** `test_onboarded_without_a_subscription_is_not_processed` and its neighbours fail because `resolve_access` still returns `entitled` for everyone. If they pass at this point, the short-circuits are already gone and something is wrong with your working tree.

- [ ] **Step 2: Restore `access.py`**

In `src/services/billing/access.py`:
- `resolve_access`: delete `return ACCESS_ENTITLED` and un-comment the body below it.
- `effective_plan_id`: delete `return PLAN_PRO` and un-comment the body below it.
- Delete the `BILLING DISABLED` paragraph from the module docstring (the four sentences from "BILLING DISABLED (temporary, for testing)" to "still run.").
- Delete the `BILLING DISABLED` note above the imports and the two `# noqa: F401` comments on the `core.plans` and `models.billing` imports — those imports are live again.
- In `may_process_mail`'s docstring, delete the closing `BILLING DISABLED:` paragraph.

- [ ] **Step 3: Restore `billing.py`**

In `src/api/v1/billing.py`:
- `_subscription_started` (line ~150): delete `return True` and un-comment the two-statement body.
- Delete the `BILLING DISABLED` paragraph from its docstring.
- Remove the `BILLING DISABLED` comment and `# noqa: F401` on the `SUBSCRIPTION_STARTED_STATUSES` import (lines 29-32).

- [ ] **Step 4: Restore `webhooks.py`**

In `src/api/v1/webhooks.py` around line 173:
- Un-comment the `if not verify_signature(...)` guard.
- Delete the `BILLING DISABLED` comment block.
- Remove `# noqa: F841` from the `signature = ...` line.
- Confirm `verify_signature` is imported in that module; if the import was removed or commented when the check was disabled, restore it.

- [ ] **Step 5: Delete the contradicting test file**

`tests/test_billing_disabled_gate.py` asserts the *disabled* behaviour — that everyone is entitled. Keeping it means the suite asserts both the paywall and its absence.

```bash
git rm tests/test_billing_disabled_gate.py
```

- [ ] **Step 6: Run the restored tests**

Run: `uv run pytest tests/test_mail_access.py tests/test_mail_sync_trigger.py -v`
Expected: PASS. This is the proof the paywall is back — these assertions are the paywall's spec.

- [ ] **Step 7: Confirm no `BILLING DISABLED` marker remains in this repo**

```bash
grep -rn "BILLING DISABLED" src/ tests/ alembic/
```

Expected: no output.

- [ ] **Step 8: Run the whole suite**

Run: `uv run pytest -v`
Expected: all pass, no skips.

- [ ] **Step 9: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add -A src/services/billing/access.py src/api/v1/billing.py src/api/v1/webhooks.py tests/
git commit -m "feat: restore the paywall

Deletes the BILLING DISABLED short-circuits in resolve_access,
effective_plan_id and _subscription_started, restores the Razorpay webhook
signature check, un-skips the two suites that assert the paywall, and
deletes test_billing_disabled_gate.py — its assertions were the inverse of
the spec and would now contradict the restored tests."
```

---

### Task 9: Restore the paywall (web)

**Repo: `inboxos-web`.** All remaining tasks are in `/Users/abcom/Desktop/openfoundry/inboxos-web`.

Restoring the API gates without this one locks users out of a dashboard the API still serves; this without the API gates leaves the paywall off. Land Task 8 and this together.

**Files:**
- Modify: `src/app/dashboard/layout.tsx:5-9, 27-63`
- Modify: `src/app/onboarding/notetaker/page.tsx:79-91`

- [ ] **Step 1: Create a branch in the web repo**

```bash
cd /Users/abcom/Desktop/openfoundry/inboxos-web
git checkout -b feat/invite-only-signups
```

- [ ] **Step 2: Restore the dashboard gate**

In `src/app/dashboard/layout.tsx`:
- Un-comment the two imports at lines 7-8 (`backendConfigured`, `getSubscription`) and delete the `BILLING DISABLED` comment above them at line 6. Merge `backendConfigured` into the existing `@/lib/session` import.
- Un-comment the `if (backendConfigured()) { ... }` block (lines ~52-63).
- Delete only the `BILLING DISABLED (temporary, for testing)` paragraph (lines ~28-34). **Keep** the long explanatory comment about `subscription_started` versus `plan_id` — it documents why the gate reads that field and is still true.

- [ ] **Step 3: Restore the wizard's exit**

In `src/app/onboarding/notetaker/page.tsx` (~line 79): delete `router.replace("/dashboard");`, un-comment `router.replace("/onboarding/plan");`, and delete the `BILLING DISABLED` paragraph. Keep the paragraph explaining why the plan picker rather than the dashboard.

- [ ] **Step 4: Confirm no marker remains**

```bash
grep -rn "BILLING DISABLED" src/
```

Expected: no output.

- [ ] **Step 5: Typecheck and build**

```bash
npm run lint && npx tsc --noEmit && npm run build
```

Expected: all clean. An unused-import error here means step 2 missed an un-comment.

- [ ] **Step 6: Commit**

```bash
git add src/app/dashboard/layout.tsx src/app/onboarding/notetaker/page.tsx
git commit -m "feat: restore the dashboard paywall gate and the wizard's plan step"
```

---

### Task 10: Trial copy 7 → 14

**Repo: `inboxos-web`.** The API now grants 14 days; five copy sites still say 7, and two of them are the Terms of Service. `Hero.tsx:24` also says 7 but its whole CTA is replaced in Task 11, so it is handled there — do not edit it here.

`TrialPill.tsx` needs no change: it derives days remaining from `sub.trial_ends_at`. That is the pattern the others should follow, and the reason this task is copy edits rather than a refactor.

**Files:**
- Modify: `src/lib/plans.ts:44`
- Modify: `src/components/marketing/Pricing.tsx:151`
- Modify: `src/components/marketing/Purpose.tsx:22`
- Modify: `src/app/(marketing)/terms/page.tsx:66,68`

- [ ] **Step 1: Find every site**

```bash
grep -rn "7-day\|7 days\|seven-day\|seven day" src/ | grep -vi "video\|transcript\|retention\|Re-sort\|last 7"
```

Expected: five hits across four files (plus `Hero.tsx`, deferred to Task 11). The `Re-sort last 7 days` and retention matches are unrelated and must not be touched.

- [ ] **Step 2: Make the edits**

| File:line | From | To |
|---|---|---|
| `src/lib/plans.ts:44` | `cta: "Start 7-day Pro trial",` | `cta: "Start 14-day Pro trial",` |
| `src/components/marketing/Pricing.tsx:151` | `The Pro trial runs 7 days with Pro&apos;s full 15 bot-hours included,` | `The Pro trial runs 14 days with Pro&apos;s full 15 bot-hours included,` |
| `src/components/marketing/Purpose.tsx:22` | `...with a seven-day trial.` | `...with a fourteen-day trial.` |
| `src/app/(marketing)/terms/page.tsx:66` | `The Pro trial runs 7 days and includes` | `The Pro trial runs 14 days and includes` |
| `src/app/(marketing)/terms/page.tsx:68` | `trial ends when the 7 days are up, not before, and card` | `trial ends when the 14 days are up, not before, and card` |

The Terms edits are a legal statement of the trial length. They must match `settings.TRIAL_DAYS` exactly.

- [ ] **Step 3: Verify nothing was missed**

```bash
grep -rn "7-day\|seven-day\|seven day" src/ | grep -vi "video\|transcript\|retention"
```

Expected: only `Hero.tsx` (Task 11). Then confirm the new copy is present:

```bash
grep -rn "14-day\|14 days\|fourteen-day" src/
```

Expected: five hits.

- [ ] **Step 4: Build and commit**

```bash
npm run lint && npx tsc --noEmit && npm run build
git add src/lib/plans.ts src/components/marketing/Pricing.tsx src/components/marketing/Purpose.tsx "src/app/(marketing)/terms/page.tsx"
git commit -m "copy: trial is 14 days, including the terms of service"
```

---

### Task 11: Point acquisition at Book-a-call, and explain the refusal

**Repo: `inboxos-web`.** Every marketing CTA currently points at `/login`: `Navbar.tsx:45` **and** `:48`, `Hero.tsx:23`, and `Pricing.tsx:26` (`return "/login"`). With signups closed, "Get started" means running the full Google consent flow — granting mailbox scopes — only to be rejected. The navbar already has the right destination at line 41: **Book a call**.

**The destination already exists as a constant.** `Navbar.tsx:36` uses `LEGAL.bookingUrl` from `src/lib/legal.ts:35` (`https://calendly.com/nilesh-pant99/30min`), documented there as "the one contact route on the site that works today". Reuse it — do **not** add a second constant holding the same URL. `Footer.tsx:34` and `BookCall.tsx:31` already import it the same way.

**Files:**
- Modify: `src/components/marketing/Navbar.tsx:44-51`
- Modify: `src/components/marketing/Hero.tsx:23-25`
- Modify: `src/components/marketing/Pricing.tsx:18-26`
- Modify: `src/app/login/page.tsx`

- [ ] **Step 1: Read how the existing CTAs pass the URL, and the Pricing comment**

```bash
sed -n 30,52p src/components/marketing/Navbar.tsx
sed -n 25,35p src/components/marketing/BookCall.tsx
sed -n 14,30p src/components/marketing/Pricing.tsx
```

`BookCall.tsx:31` shows the house pattern for an external `Button`: `<Button variant="primary" href={LEGAL.bookingUrl} external>`. Use that `external` prop rather than hand-writing `target`/`rel`.

Read `Pricing.tsx:18`'s comment: it documents the deliberate decision to funnel pricing through `/login`. That reasoning is being reversed, so the comment gets rewritten, not contradicted.

- [ ] **Step 2: Repoint the CTAs**

Each file imports the constant it already has available: `import { LEGAL } from "@/lib/legal";`

- `Navbar.tsx:48` — the `Get started` button: `href="/login"` → `href={LEGAL.bookingUrl}`, and add the `external` prop. Leave the `Log in` button at line 45 pointing at `/login`. (`LEGAL` is already imported in this file for line 36.)
- `Hero.tsx:23-24` — `href="/login"` → `href={LEGAL.bookingUrl}` with `external`, and the label `Start 7-day Pro trial` → `Request access`. (This is the `Hero.tsx` change deferred from Task 10.)
- `Pricing.tsx:26` — `return "/login";` → `return LEGAL.bookingUrl;`, and rewrite the comment at line 18: signups are invite-only, so a plan CTA that led to a refused login would be worse than one that leads to a conversation. Check whether the value is consumed somewhere that assumes an internal path — if the caller renders a `Button` without `external`, add it there too.

- [ ] **Step 3: Explain the refusal on `/login`**

`src/app/login/page.tsx` is already `"use client"`. Add `useSearchParams` and render a notice when `?error=not_invited` — the exact value `src/api/v1/auth.py` redirects with (Task 5).

Add to the imports:

```tsx
import { useSearchParams } from "next/navigation";
import { LEGAL } from "@/lib/legal";
```

Inside the component, beside `const configured = backendConfigured();`:

```tsx
  const notInvited = useSearchParams().get("error") === "not_invited";
```

And directly above the `<div className="mt-8 space-y-3">` button block:

```tsx
        {notInvited && (
          <p className="mt-4 rounded-lg bg-ink/5 p-3 text-sm leading-relaxed text-ink/70">
            InboxPilot is invite-only while we onboard our first users.{" "}
            <a href={LEGAL.bookingUrl} target="_blank" rel="noreferrer noopener" className="underline">
              Book a call
            </a>{" "}
            and we&apos;ll get you set up.
          </p>
        )}
```

`useSearchParams` needs a Suspense boundary during static rendering. If `npm run build` complains, wrap the page body in a child component and render it inside `<Suspense fallback={null}>` from the default export — do not switch the page to dynamic rendering.

- [ ] **Step 4: Build**

```bash
npm run lint && npx tsc --noEmit && npm run build
```

Expected: clean.

- [ ] **Step 5: Verify no acquisition CTA still points at /login**

```bash
grep -rn '"/login"' src/components/marketing/ src/lib/
```

Expected: exactly one hit — `Navbar.tsx:45`, the `Log in` button. Anything else is a dead end for an uninvited visitor.

- [ ] **Step 6: See it for real**

```bash
npm run dev
```

Open `http://localhost:3000/login?error=not_invited` and confirm the notice renders with a working Book-a-call link. Open `/` and confirm "Get started", the hero CTA, and the pricing CTAs all go to the call booking rather than `/login`.

- [ ] **Step 7: Commit**

```bash
git add src/components/marketing/Navbar.tsx src/components/marketing/Hero.tsx src/components/marketing/Pricing.tsx src/app/login/page.tsx
git commit -m "feat: funnel acquisition through book-a-call while signups are invite-only

Every marketing CTA pointed at /login, so with the invite gate on, 'Get
started' meant completing Google's consent screen only to be refused. /login
keeps its Log in entry and now explains the refusal when the callback sends
someone back with ?error=not_invited."
```

---

## Final verification

- [ ] **API suite green, no skips:** `cd /Users/abcom/Desktop/openfoundry/InboxPilot && uv run pytest -v`
- [ ] **No marker left in either repo:** `grep -rn "BILLING DISABLED" .` in both — no output
- [ ] **Migrations at head and reversible to `c8e2f4a10b57`:** `uv run alembic heads` → `c8e2f4a10b57 (head)`
- [ ] **Web builds:** `cd /Users/abcom/Desktop/openfoundry/inboxos-web && npm run build`
- [ ] **`LOGIN_URL` is set in every deployed environment** before this ships, or a refused signup redirects to localhost. Check Render, and `terraform plan` for ECS.
- [ ] **Add the invited addresses as Google OAuth test users** while the app is unverified, so uninvited visitors are stopped before reaching the callback.

## Known gaps carried from the spec

- Nothing reads `User.is_active`; there is no way to revoke a user who is already in. `invite.py revoke` deliberately refuses claimed invites for this reason.
- Trial length now lives in six places (config plus five copy sites). The durable fix — serve it from the API and render it, as `TrialPill.tsx` already does — is deferred.
- Google's test-user list and `invited_emails` are maintained by hand from the same source.
