# Categorization API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace InboxPilot's hardcoded six-category email classifier with a per-user, DB-backed taxonomy plus deterministic rules, classifier tuning, per-category Gmail actions, and an on-demand re-classify job — the backend for the Categorization page's General and Advanced tabs.

**Architecture:** Three new tables (`email_categories`, `categorization_rules`, `categorization_settings`) hold per-user config, seeded from a `BUILTIN_CATEGORIES` constant that becomes the single source of truth for the six organizational labels. A new `services/categorization/pipeline.py` replaces the body of `services/classify/apply.py`: it loads config from the DB inside the Celery worker, runs deterministic rules first (which can skip the LLM entirely), falls through to the LLM with a user-specific prompt, then applies the label and its actions in one Composio call. A thin FastAPI router exposes CRUD over all three tables.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic, Celery, Pydantic v2, Composio (Gmail), OpenAI.

**Spec:** `docs/superpowers/specs/2026-07-27-categorization-api-design.md`

## Global Constraints

- **No tests.** Deferred by explicit decision. Every task ends with a lint gate and a runtime check instead of a red-green cycle. Do not create a `tests/` directory.
- **`make lint` is broken on this repo and is not the gate.** It runs `ruff check src tests`, and `tests/` does not exist, so it exits with `E902 No such file or directory`. Use `uv run ruff check <files you touched>` instead.
- **Pre-existing lint debt is not yours to fix.** Baseline on `main`: 4 ruff errors in `src` (in `services/meetings/recap.py` and `workers/jobs/routines_sweep.py`) and 31 mypy errors across 15 files. Do not fix them; do not let them block you. Only assert that files *you* touched are clean.
- **Imports are rooted at `src/`.** The package root is `src`, so imports read `from models.categorization import ...`, never `from src.models...`. Run Python as `PYTHONPATH=src uv run python ...`.
- **Layering:** `models/` may not import from `services/`. `integrations/` may not import from `services/`. This is why `BUILTIN_CATEGORIES` lives in `models/categorization.py` — both `services/categorization/store.py` and `integrations/composio/gmail.py` need it.
- **Composio `user_id` is `str(User.id)`.** The app user's UUID, passed as a string to every Composio call.
- **Gmail label names are immutable.** There is no rename action in Composio. `EmailCategory.key` and `EmailCategory.gmail_label` are set once at creation and never updated; only `display_name` changes.
- **No writes to the real Gmail account, and no OpenAI spend.** The dev stack is live and backed by a real connected mailbox. You may read from the DB and call the API, but you may NOT run any path that reaches `gmail.create_label`, `gmail_ops._modify`, or the classifier against real data — that means no happy-path `POST /categories`, and never calling `POST /reclassify`. Prove those paths with `unittest.mock.patch` instead, as Task 10's verification step already does.
- **Getting a token for API smoke checks** (no browser needed):
  ```bash
  TOKEN=$(docker compose exec -T api python -c "from core.security import create_access_token; print(create_access_token('e397bee9-17ed-40d1-a3a0-0b55e115dc90'))" | tr -d '\r')
  ```
  That user id is the only row in the dev `users` table. Verify with `curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" http://localhost:8000/v1/auth/me` → `200`.
- **The dev stack is already running** (`docker compose ps`: api, worker, beat, db, redis, rabbitmq, up for hours). Do not `make up` or `make down`. `make migrate` is fine.
- **Branch:** work on `feat/categorization-api`, already created and holding the spec commit.
- **Commit style:** Conventional Commits (`feat:`, `refactor:`, `docs:`). Match the existing log.

## File Structure

**Create:**
| File | Responsibility |
|---|---|
| `src/models/categorization.py` | The three ORM models, the match/action string constants, and `BUILTIN_CATEGORIES`. No behaviour. |
| `src/services/categorization/__init__.py` | Empty package marker. |
| `src/services/categorization/store.py` | Async DB access: seeding, get-or-create, lookups. No HTTP, no Composio. |
| `src/services/categorization/pipeline.py` | The classify-and-apply decision path used by workers. Sync (Celery-facing), owns the DB→config load. |
| `src/services/categorization/rules.py` | Pure rule matching. No I/O — the one genuinely testable unit if tests are ever added. |
| `src/services/categorization/backfill.py` | Shared "fetch recent, skip already-labelled, enqueue" helper. |
| `src/schemas/categorization.py` | Pydantic request/response models. |
| `src/api/v1/categorization.py` | The router. Validation and HTTP status mapping only. |
| `src/workers/jobs/reclassify.py` | The `categorization.reclassify` Celery task. |
| `alembic/versions/<rev>_add_categorization_tables.py` | One migration, three tables. |

**Modify:**
| File | Change |
|---|---|
| `src/integrations/composio/gmail.py:40-53` | Split `INBOXPILOT_LABELS` into `INTERNAL_LABELS` + a derivation from `BUILTIN_CATEGORIES`. Add `RESERVED_LABEL_NAMES`. |
| `src/services/classify/classifier.py` | Taxonomy becomes a parameter; returns a `Verdict` with confidence; `LABELS`/`LABEL_NAMES` deleted. |
| `src/services/classify/apply.py` | Delegates to `pipeline.categorize_and_apply`. |
| `src/services/mailman/gmail_ops.py:11-13` | Add `UNREAD_LABEL` / `STARRED_LABEL`; add `apply_category`. |
| `src/workers/jobs/sync_last_7_days.py:60-77` | Use the shared backfill helper. |
| `src/api/router.py` | Register the categorization router. |
| `src/core/config.py:49-50` | Add `CLASSIFIER_MODELS`. |
| `alembic/env.py:14-20` | Import the new model module. |

---

# Phase 1 — General tab

At the end of Phase 1 the General tab is fully usable, and behaviour for a user who changes nothing is identical to today's.

---

### Task 1: Models and migration

**Files:**
- Create: `src/models/categorization.py`
- Create: `alembic/versions/<rev>_add_categorization_tables.py` (generated)
- Modify: `alembic/env.py`

**Interfaces:**
- Consumes: `models.base.Base`, `UUIDMixin`, `TimestampMixin`
- Produces: `EmailCategory`, `CategorizationRule`, `CategorizationSettings`, `BuiltinCategory`, `BUILTIN_CATEGORIES`, `default_actions()`, `CATEGORY_ACTIONS`, and the constants `MATCH_SENDER_ADDRESS` / `MATCH_SENDER_DOMAIN` / `MATCH_SUBJECT_KEYWORD` / `MATCH_BODY_KEYWORD` / `MATCH_TYPES` / `RULE_ASSIGN` / `RULE_EXCLUDE` / `RULE_ACTIONS`

- [ ] **Step 1: Write `src/models/categorization.py`**

```python
"""Per-user email categorization: taxonomy, deterministic rules, tuning knobs.

Three tables. `email_categories` is the user's taxonomy — the six built-ins are
seeded on first read and can be renamed, recoloured, disabled, or joined by
custom ones. `categorization_rules` are deterministic matches evaluated before
the LLM is ever called. `categorization_settings` is the per-user singleton of
tuning knobs.

`BUILTIN_CATEGORIES` lives here rather than in the service layer because
`integrations.composio.gmail` also needs it to provision Gmail labels, and
integrations may not import from services.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

# What a rule can look at. `body_keyword` matches the *snippet*: the Gmail
# trigger payload carries only sender/subject/snippet and we never re-fetch.
MATCH_SENDER_ADDRESS = "sender_address"
MATCH_SENDER_DOMAIN = "sender_domain"
MATCH_SUBJECT_KEYWORD = "subject_keyword"
MATCH_BODY_KEYWORD = "body_keyword"
MATCH_TYPES = frozenset(
    {MATCH_SENDER_ADDRESS, MATCH_SENDER_DOMAIN, MATCH_SUBJECT_KEYWORD, MATCH_BODY_KEYWORD}
)

# What a matching rule does.
RULE_ASSIGN = "assign"
RULE_EXCLUDE = "exclude"
RULE_ACTIONS = frozenset({RULE_ASSIGN, RULE_EXCLUDE})

# Per-category Gmail side effects. Archive and skip-inbox are the same mutation
# (remove INBOX), so there is one key, not two.
CATEGORY_ACTIONS = ("archive", "mark_read", "star")


def default_actions() -> dict[str, bool]:
    return {name: False for name in CATEGORY_ACTIONS}


@dataclass(frozen=True)
class BuiltinCategory:
    key: str
    gmail_label: str
    display_name: str
    description: str
    color_bg: str
    color_text: str


# The single source of truth for the six organizational categories. Seeds every
# user's taxonomy; `gmail.INBOXPILOT_LABELS` derives its colours from it. The
# `gmail_label` values MUST stay exactly as they are — they name labels that
# already exist in users' mailboxes and carry already-classified mail.
BUILTIN_CATEGORIES: tuple[BuiltinCategory, ...] = (
    BuiltinCategory(
        key="to_do",
        gmail_label="to do",
        display_name="To do",
        description=(
            "Needs an action or reply from me; a real request, task, or question "
            "directed at me."
        ),
        color_bg="#fb4c2f",
        color_text="#ffffff",
    ),
    BuiltinCategory(
        key="to_follow_up",
        gmail_label="to follow up",
        display_name="To follow up",
        description=(
            "A thread I'm waiting on or should chase; awaiting someone's reply, "
            "or a nudge I must track."
        ),
        color_bg="#a479e2",
        color_text="#ffffff",
    ),
    BuiltinCategory(
        key="notification",
        gmail_label="notification",
        display_name="Notification",
        description=(
            "Automated transactional notice: receipts, confirmations, alerts, "
            "security codes, system messages."
        ),
        color_bg="#4a86e8",
        color_text="#ffffff",
    ),
    BuiltinCategory(
        key="fyi",
        gmail_label="fyi",
        display_name="FYI",
        description=(
            "Informational and relevant, from a person or team, but needs no "
            "action from me."
        ),
        color_bg="#16a766",
        color_text="#ffffff",
    ),
    BuiltinCategory(
        key="marketing",
        gmail_label="marketing",
        display_name="Marketing",
        description=(
            "Promotional or sales: newsletters, product offers, campaigns, cold pitches."
        ),
        color_bg="#fad165",
        color_text="#000000",
    ),
    BuiltinCategory(
        key="noise",
        gmail_label="noise",
        display_name="Noise",
        description="Low-value bulk or social clutter; spam-like, unimportant, safe to ignore.",
        color_bg="#999999",
        color_text="#ffffff",
    ),
)


class EmailCategory(UUIDMixin, TimestampMixin, Base):
    """One category in a user's taxonomy. `key` and `gmail_label` never change."""

    __tablename__ = "email_categories"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_email_categories_user_key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    gmail_label: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    color_bg: Mapped[str] = mapped_column(String(7), default="#999999", nullable=False)
    color_text: Mapped[str] = mapped_column(String(7), default="#ffffff", nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions: Mapped[dict] = mapped_column(JSONB, default=default_actions, nullable=False)

    def __repr__(self) -> str:
        return f"<EmailCategory {self.key}>"


class CategorizationRule(UUIDMixin, TimestampMixin, Base):
    """A deterministic match evaluated before the LLM. Lower priority runs first."""

    __tablename__ = "categorization_rules"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    match_value: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(16), default=RULE_ASSIGN, nullable=False)

    # References EmailCategory.key. Not an FK: `key` is unique only per user, so
    # this would need a composite (user_id, key) reference for no real benefit.
    # Validated in the API layer; cleaned up when a category is deleted.
    category_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CategorizationSettings(UUIDMixin, TimestampMixin, Base):
    """Per-user singleton, same shape as MailmanSettings / MeetingSettings."""

    __tablename__ = "categorization_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # None = leave undecided mail unlabelled, which is today's behaviour.
    fallback_category_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 0.0 = never override the model's pick.
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # None = use settings.OPENAI_MODEL.
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_reclassify_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 2: Register the module with Alembic**

In `alembic/env.py`, add to the block at lines 14-20, keeping alphabetical order:

```python
from models import categorization as categorization_models  # noqa: F401
```

- [ ] **Step 3: Verify the models load and register their tables**

Run:
```bash
PYTHONPATH=src uv run python -c "
from models.base import Base
from models import categorization  # noqa: F401
names = sorted(t for t in Base.metadata.tables if 'categor' in t)
print(names)
from models.categorization import BUILTIN_CATEGORIES
print(len(BUILTIN_CATEGORIES), [c.key for c in BUILTIN_CATEGORIES])
"
```
Expected, exactly:
```
['categorization_rules', 'categorization_settings', 'email_categories']
6 ['to_do', 'to_follow_up', 'notification', 'fyi', 'marketing', 'noise']
```

- [ ] **Step 4: Generate the migration**

Run:
```bash
make revision m="add categorization tables"
```
Then open the generated file in `alembic/versions/`. Confirm it creates exactly three tables and no others, and that `upgrade()` contains no `op.drop_table` for any pre-existing table. If autogenerate has swept in unrelated drops (it will if your DB is behind), run `make migrate` first and regenerate.

- [ ] **Step 5: Apply the migration**

Run:
```bash
make migrate
```
Expected: `Running upgrade ... -> <rev>, add categorization tables`, exit 0.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/models/categorization.py alembic/env.py
git add src/models/categorization.py alembic/env.py alembic/versions/
git commit -m "feat: add categorization tables and builtin taxonomy"
```

---

### Task 2: Store, and collapsing the duplicated taxonomy

**Files:**
- Create: `src/services/categorization/__init__.py`, `src/services/categorization/store.py`
- Modify: `src/integrations/composio/gmail.py:40-53`

**Interfaces:**
- Consumes: `models.categorization.*` from Task 1
- Produces: `get_or_create_categories(db, user_id) -> list[EmailCategory]`, `get_category(db, user_id, key) -> EmailCategory | None`, `get_or_create_settings(db, user_id) -> CategorizationSettings`, and in `gmail`: `INTERNAL_LABELS`, `RESERVED_LABEL_NAMES`

- [ ] **Step 1: Create the package marker**

```bash
touch src/services/categorization/__init__.py
```

- [ ] **Step 2: Write `src/services/categorization/store.py`**

```python
"""Async DB access for the categorization taxonomy, rules, and settings.

Get-or-create throughout, matching `services.mailman.store`: the first read of a
user's taxonomy seeds the six built-ins, so nothing has to happen at signup.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.categorization import (
    BUILTIN_CATEGORIES,
    CategorizationSettings,
    EmailCategory,
    default_actions,
)


async def get_or_create_categories(db: AsyncSession, user_id: uuid.UUID) -> list[EmailCategory]:
    """Return the user's taxonomy in display order, seeding built-ins on first call."""
    rows = list(
        await db.scalars(
            select(EmailCategory)
            .where(EmailCategory.user_id == user_id)
            .order_by(EmailCategory.sort_order, EmailCategory.key)
        )
    )
    if rows:
        return rows

    for index, builtin in enumerate(BUILTIN_CATEGORIES):
        db.add(
            EmailCategory(
                user_id=user_id,
                key=builtin.key,
                gmail_label=builtin.gmail_label,
                display_name=builtin.display_name,
                description=builtin.description,
                color_bg=builtin.color_bg,
                color_text=builtin.color_text,
                is_builtin=True,
                is_enabled=True,
                sort_order=index,
                actions=default_actions(),
            )
        )
    await db.flush()

    return list(
        await db.scalars(
            select(EmailCategory)
            .where(EmailCategory.user_id == user_id)
            .order_by(EmailCategory.sort_order, EmailCategory.key)
        )
    )


async def get_category(
    db: AsyncSession, user_id: uuid.UUID, key: str
) -> EmailCategory | None:
    return await db.scalar(
        select(EmailCategory).where(
            EmailCategory.user_id == user_id, EmailCategory.key == key
        )
    )


async def get_or_create_settings(
    db: AsyncSession, user_id: uuid.UUID
) -> CategorizationSettings:
    row = await db.scalar(
        select(CategorizationSettings).where(CategorizationSettings.user_id == user_id)
    )
    if row is None:
        row = CategorizationSettings(user_id=user_id)
        db.add(row)
        await db.flush()
    return row
```

- [ ] **Step 3: Rewire `INBOXPILOT_LABELS` in `src/integrations/composio/gmail.py`**

Replace the whole `INBOXPILOT_LABELS` literal at lines 40-53 with this. The six category entries now derive from `BUILTIN_CATEGORIES`, so their names and colours cannot drift from the classifier's taxonomy.

```python
# Labels InboxPilot uses internally; not part of the user's taxonomy and not
# editable from the Categorization page.
INTERNAL_LABELS: dict[str, dict[str, str]] = {
    "inboxos-chat": {"background_color": "#2da2bb", "text_color": "#ffffff", "label_list_visibility": "labelShowIfUnread"},  # teal
    "inboxos-routines": {"background_color": "#ffad47", "text_color": "#000000", "label_list_visibility": "labelShowIfUnread"},  # orange
    "inboxos-later": {"background_color": "#f691b3", "text_color": "#000000", "label_list_visibility": "labelShowIfUnread"},  # pink
    "inboxos-rules": {"background_color": "#efa093", "text_color": "#000000", "label_list_visibility": "labelShowIfUnread"},  # salmon
}

# The org labels every account gets provisioned, derived from the one taxonomy
# definition so colours and names cannot drift from what the classifier uses.
INBOXPILOT_LABELS: dict[str, dict[str, str]] = {
    **{
        builtin.gmail_label: {
            "background_color": builtin.color_bg,
            "text_color": builtin.color_text,
        }
        for builtin in BUILTIN_CATEGORIES
    },
    **INTERNAL_LABELS,
}

# Names a user may not claim for a custom category.
RESERVED_LABEL_NAMES: frozenset[str] = frozenset(
    name.casefold() for name in INBOXPILOT_LABELS
)
```

Add the import at the top of the file, with the other `from` imports:

```python
from models.categorization import BUILTIN_CATEGORIES
```

- [ ] **Step 4: Verify the derived label set is unchanged**

The point of this refactor is that the provisioned labels are byte-identical to before. Run:

```bash
PYTHONPATH=src uv run python -c "
from integrations.composio.gmail import INBOXPILOT_LABELS, RESERVED_LABEL_NAMES
before = {
    'to do': ('#fb4c2f', '#ffffff'), 'notification': ('#4a86e8', '#ffffff'),
    'fyi': ('#16a766', '#ffffff'), 'marketing': ('#fad165', '#000000'),
    'noise': ('#999999', '#ffffff'), 'to follow up': ('#a479e2', '#ffffff'),
    'inboxos-chat': ('#2da2bb', '#ffffff'), 'inboxos-routines': ('#ffad47', '#000000'),
    'inboxos-later': ('#f691b3', '#000000'), 'inboxos-rules': ('#efa093', '#000000'),
}
now = {k: (v['background_color'], v['text_color']) for k, v in INBOXPILOT_LABELS.items()}
assert now == before, f'label set drifted:\n  {sorted(now.items())}\n  {sorted(before.items())}'
assert len(RESERVED_LABEL_NAMES) == 10, RESERVED_LABEL_NAMES
print('label set identical to pre-refactor:', len(now), 'labels')
"
```
Expected: `label set identical to pre-refactor: 10 labels`

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/services/categorization/ src/integrations/composio/gmail.py
git add src/services/categorization/ src/integrations/composio/gmail.py
git commit -m "refactor: derive gmail org labels from one taxonomy definition"
```

---

### Task 3: Schemas and the General-tab endpoints

**Files:**
- Create: `src/schemas/categorization.py`, `src/api/v1/categorization.py`
- Modify: `src/api/router.py`

**Interfaces:**
- Consumes: `store.get_or_create_categories`, `store.get_category`, `store.get_or_create_settings` from Task 2
- Produces: routes `GET /v1/categorization/categories`, `PATCH /v1/categorization/categories/{key}`, `GET /v1/categorization/settings`, `PUT /v1/categorization/settings`; schemas `CategoryActions`, `CategoryRead`, `CategoryUpdate`, `SettingsRead`, `SettingsUpdate`

`SettingsRead`/`SettingsUpdate` carry only `is_enabled` in this task. Task 9 widens them with the tuning knobs.

- [ ] **Step 1: Write `src/schemas/categorization.py`**

```python
"""Pydantic schemas for the Categorization API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

HEX_COLOR = r"^#[0-9a-fA-F]{6}$"


class CategoryActions(BaseModel):
    """Gmail side effects applied alongside a category's label."""

    archive: bool = False
    mark_read: bool = False
    star: bool = False


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    gmail_label: str
    display_name: str
    description: str
    color_bg: str
    color_text: str
    is_builtin: bool
    is_enabled: bool
    sort_order: int
    actions: CategoryActions


class CategoryUpdate(BaseModel):
    """Partial update. `key` and `gmail_label` are absent by design — Gmail has
    no rename-label action, so they are fixed at creation."""

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1)
    color_bg: str | None = Field(default=None, pattern=HEX_COLOR)
    color_text: str | None = Field(default=None, pattern=HEX_COLOR)
    is_enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0)
    actions: CategoryActions | None = None


class SettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    is_enabled: bool
    last_reclassify_at: datetime | None = None


class SettingsUpdate(BaseModel):
    is_enabled: bool | None = None
```

- [ ] **Step 2: Write `src/api/v1/categorization.py`**

```python
"""Categorization API — the user's taxonomy, rules, and classifier settings.

Backs the Categorization page. The General tab reads and edits the six built-in
categories; the Advanced tab adds custom categories, deterministic rules, and
tuning knobs.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from api.deps import DbSession
from models.categorization import CategorizationSettings, EmailCategory, default_actions
from models.users import User
from schemas.categorization import (
    CategoryRead,
    CategoryUpdate,
    SettingsRead,
    SettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.categorization.store import (
    get_category,
    get_or_create_categories,
    get_or_create_settings,
)

router = APIRouter(prefix="/categorization", tags=["categorization"])

CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/categories", response_model=list[CategoryRead])
async def list_categories(user: CurrentUser, db: DbSession) -> list[EmailCategory]:
    """The user's taxonomy in display order; seeds the built-ins on first call."""
    return await get_or_create_categories(db, user.id)


@router.patch("/categories/{key}", response_model=CategoryRead)
async def update_category(
    key: str, payload: CategoryUpdate, user: CurrentUser, db: DbSession
) -> EmailCategory:
    # Seed first: a user who PATCHes before ever listing still has a taxonomy.
    await get_or_create_categories(db, user.id)

    category = await get_category(db, user.id, key)
    if category is None:
        raise HTTPException(404, f"no category with key {key!r}")

    data = payload.model_dump(exclude_unset=True)
    if "actions" in data:
        # exclude_unset recurses into nested models, so a one-checkbox PATCH
        # arrives as {"archive": True} and a plain assignment would wipe the
        # other two flags. Merge over default_actions() so a row that is
        # already missing keys self-heals.
        data["actions"] = {**default_actions(), **(category.actions or {}), **data["actions"]}

    for field, value in data.items():
        setattr(category, field, value)
    return category


@router.get("/settings", response_model=SettingsRead)
async def get_settings(user: CurrentUser, db: DbSession) -> CategorizationSettings:
    return await get_or_create_settings(db, user.id)


@router.put("/settings", response_model=SettingsRead)
async def update_settings(
    payload: SettingsUpdate, user: CurrentUser, db: DbSession
) -> CategorizationSettings:
    row = await get_or_create_settings(db, user.id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    return row
```

`payload.model_dump()` flattens the nested `CategoryActions` to a plain dict, which is what the `JSONB` column wants. But note the merge above: `exclude_unset=True` recurses, so a nested model is dumped with only the keys the client actually sent. Assigning that dict straight to the column is data loss, not a partial update.

- [ ] **Step 3: Register the router in `src/api/router.py`**

```python
from api.v1 import auth, categorization, chat, integrations, mailman, users, webhooks, meetings
```

and, after `api_router.include_router(mailman.router)`:

```python
api_router.include_router(categorization.router)
```

- [ ] **Step 4: Verify the routes are mounted**

Run:
```bash
PYTHONPATH=src uv run python -c "
from main import app
# NOTE: do not scan app.routes — FastAPI 0.139 keeps included routers as lazy
# _IncludedRouter objects, so a flat scan finds nothing. The OpenAPI schema
# forces resolution and is the public contract anyway.
spec = app.openapi()
found = {p: sorted(m.upper() for m in ops) for p, ops in spec['paths'].items() if 'categorization' in p}
for path, methods in sorted(found.items()):
    print(f'{\",\".join(methods):14} {path}')
assert sum(len(m) for m in found.values()) == 4, found
"
```
Expected, exactly:
```
GET          /v1/categorization/categories
PATCH        /v1/categorization/categories/{key}
GET,PUT      /v1/categorization/settings
```
(Three route entries — `GET` and `PUT` on `/settings` share one path.)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/schemas/categorization.py src/api/v1/categorization.py src/api/router.py
git add src/schemas/categorization.py src/api/v1/categorization.py src/api/router.py
git commit -m "feat: categorization taxonomy and settings endpoints"
```

---

### Task 4: DB-backed classification pipeline

This is the task that actually changes runtime behaviour. Until now the tables existed but nothing read them.

**Files:**
- Create: `src/services/categorization/pipeline.py`
- Modify: `src/services/classify/classifier.py`, `src/services/classify/apply.py`

**Interfaces:**
- Consumes: `store.get_or_create_categories`, `store.get_or_create_settings`; `core.database.run_async` / `with_worker_session`; `services.mailman.gmail_ops.add_label`
- Produces: `classifier.Category`, `classifier.Verdict`, `classifier.classify(sender, subject, snippet, *, categories, model=None, extra_instructions=None) -> Verdict`; `pipeline.UserConfig`, `pipeline.load_config(db, user_id) -> UserConfig`, `pipeline.categorize_and_apply(user_id, *, message_id, sender, subject, snippet) -> str | None`

`Verdict` carries `confidence` from this task onward, but nothing consumes it until Task 9. That is deliberate — it keeps the classifier's signature stable across the two phases.

- [ ] **Step 1: Rewrite `src/services/classify/classifier.py`**

Replace the file entirely. `LABELS` and `LABEL_NAMES` are deleted; the taxonomy now arrives as an argument.

```python
"""LLM classification of an email into one of the user's categories.

The taxonomy is a parameter, not a constant: each user has their own set of
categories with their own names and guidance (see `models.categorization`).
Blocking OpenAI call — invoke from a worker.
"""

import json
from dataclasses import dataclass
from functools import lru_cache

from openai import OpenAI

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Category:
    """One choice offered to the model. `key` is what comes back out."""

    key: str
    display_name: str
    description: str


@dataclass(frozen=True)
class Verdict:
    key: str | None
    confidence: float


NO_VERDICT = Verdict(key=None, confidence=0.0)


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _system_prompt(categories: list[Category], extra_instructions: str | None) -> str:
    lines = [f'- "{c.display_name}": {c.description}' for c in categories]
    prompt = (
        "You classify an incoming email into exactly one category for a busy user. "
        "Categories:\n" + "\n".join(lines) + "\n\n"
        'Respond ONLY as JSON: {"label": "<one category name exactly as written above>", '
        '"confidence": <number between 0 and 1>}. '
        "Pick the single best fit. When unsure between an actionable and an informational "
        "category, prefer the actionable one. `confidence` is how sure you are of the pick."
    )
    if extra_instructions:
        prompt += f"\n\nAdditional instructions from the user:\n{extra_instructions.strip()}"
    return prompt


def classify(
    sender: str | None,
    subject: str | None,
    snippet: str | None,
    *,
    categories: list[Category],
    model: str | None = None,
    extra_instructions: str | None = None,
) -> Verdict:
    """Return the chosen category key and confidence, or NO_VERDICT."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    if not categories:
        return NO_VERDICT

    user = (
        f"From: {sender or '(unknown)'}\n"
        f"Subject: {subject or '(no subject)'}\n"
        f"Preview: {(snippet or '')[:500]}"
    )
    resp = _client().chat.completions.create(
        model=model or settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _system_prompt(categories, extra_instructions)},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
        label = parsed.get("label")
        confidence = float(parsed.get("confidence", 1.0))
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        log.warning("classify.bad_json", raw=raw)
        return NO_VERDICT

    # The model answers with a display name; map it back to a stable key. Be
    # lenient about case and surrounding whitespace before giving up.
    by_name = {c.display_name.strip().casefold(): c.key for c in categories}
    key = by_name.get((label or "").strip().casefold())
    if key is None:
        log.warning("classify.unknown_label", label=label)
        return NO_VERDICT

    return Verdict(key=key, confidence=max(0.0, min(1.0, confidence)))
```

- [ ] **Step 2: Write `src/services/categorization/pipeline.py`**

```python
"""Decide a message's category and apply it. The one path both callers use.

Sync by design: Celery tasks are sync, so the DB read goes through
`run_async(with_worker_session(...))` — a loop-local session, per
`core.database`. Phase 1 is master-switch plus LLM; the rules pass and the
per-category actions arrive in Phase 2.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from core.database import run_async, with_worker_session
from core.logging import get_logger
from services.categorization.store import get_or_create_categories, get_or_create_settings
from services.classify.classifier import Category, classify
from services.mailman import gmail_ops

log = get_logger(__name__)


@dataclass(frozen=True)
class CategorySnapshot:
    """A category as the pipeline needs it, detached from the DB session."""

    key: str
    gmail_label: str
    display_name: str
    description: str
    is_enabled: bool
    actions: dict


@dataclass(frozen=True)
class UserConfig:
    is_enabled: bool
    categories: tuple[CategorySnapshot, ...]

    def enabled(self) -> list[CategorySnapshot]:
        return [c for c in self.categories if c.is_enabled]

    def by_key(self, key: str) -> CategorySnapshot | None:
        return next((c for c in self.categories if c.key == key), None)


async def load_config(db: AsyncSession, user_id: uuid.UUID) -> UserConfig:
    settings_row = await get_or_create_settings(db, user_id)
    categories = await get_or_create_categories(db, user_id)
    return UserConfig(
        is_enabled=settings_row.is_enabled,
        categories=tuple(
            CategorySnapshot(
                key=c.key,
                gmail_label=c.gmail_label,
                display_name=c.display_name,
                description=c.description,
                is_enabled=c.is_enabled,
                actions=dict(c.actions or {}),
            )
            for c in categories
        ),
    )


def get_config(user_id: str) -> UserConfig:
    """Load a user's categorization config from sync (Celery) code."""
    uid = uuid.UUID(user_id)
    return run_async(with_worker_session(lambda db: load_config(db, uid)))


def categorize_and_apply(
    user_id: str,
    *,
    message_id: str,
    sender: str | None,
    subject: str | None,
    snippet: str | None,
) -> str | None:
    """Categorize one message and apply the result. Returns the key, or None."""
    config = get_config(user_id)
    if not config.is_enabled:
        log.info("categorize.disabled", user_id=user_id, message_id=message_id)
        return None

    enabled = config.enabled()
    if not enabled:
        log.info("categorize.no_categories", user_id=user_id, message_id=message_id)
        return None

    verdict = classify(
        sender,
        subject,
        snippet,
        categories=[
            Category(key=c.key, display_name=c.display_name, description=c.description)
            for c in enabled
        ],
    )
    if verdict.key is None:
        return None

    category = config.by_key(verdict.key)
    if category is None:
        return None

    gmail_ops.add_label(user_id, [message_id], category.gmail_label)
    log.info(
        "categorize.applied",
        user_id=user_id,
        message_id=message_id,
        category=category.key,
        confidence=verdict.confidence,
    )
    return category.key
```

- [ ] **Step 3: Rewrite `src/services/classify/apply.py`**

The label-provisioning cache stays: it covers the built-ins, which the taxonomy always starts from. Custom categories create their Gmail label at API time (Task 6), so this cache never needs invalidating.

```python
"""Classify one message and apply the resulting category label.

The single implementation shared by the webhook task (mail arriving now) and the
onboarding backfill (mail that arrived before the user connected). Blocking
Composio + OpenAI calls — invoke from a Celery task.
"""

from functools import lru_cache

from core.logging import get_logger
from integrations.composio import gmail
from services.categorization import pipeline

log = get_logger(__name__)


@lru_cache(maxsize=256)
def _ensure_labels_once(user_id: str) -> bool:
    """Provision the built-in org labels for a user, at most once per process.

    The classifier can only apply a label that exists in the user's Gmail.
    Accounts that skipped (or failed) the initial sync would otherwise fail with
    `label '<name>' not found`. Idempotent and cheap (one LIST plus creates for
    whatever is missing). A raised error is not cached, so it retries next time.

    Custom categories are not covered here — their Gmail label is created
    synchronously when the category is created, so this cache never goes stale.
    """
    gmail.ensure_labels(user_id)
    return True


def classify_and_label(
    user_id: str,
    *,
    message_id: str,
    sender: str | None,
    subject: str | None,
    snippet: str | None,
) -> str | None:
    """Label one message. Returns the category key applied, or None."""
    _ensure_labels_once(user_id)
    return pipeline.categorize_and_apply(
        user_id,
        message_id=message_id,
        sender=sender,
        subject=subject,
        snippet=snippet,
    )
```

- [ ] **Step 4: Fix the one remaining importer of the deleted constant**

`src/workers/jobs/sync_last_7_days.py:14` imports `LABEL_NAMES`, which no longer exists. Task 5 rewrites this function properly; for now, make it import-clean by replacing line 14:

```python
from models.categorization import BUILTIN_CATEGORIES
```

and line 60's body — replace:

```python
    known = {lid for name in LABEL_NAMES if (lid := gmail_ops.resolve_label_id(user_id, name))}
```

with:

```python
    names = [c.gmail_label for c in BUILTIN_CATEGORIES]
    known = {lid for name in names if (lid := gmail_ops.resolve_label_id(user_id, name))}
```

- [ ] **Step 5: Verify nothing still references the deleted names, and everything imports**

Run:
```bash
grep -rn "LABEL_NAMES\|classifier.LABELS\|from services.classify.classifier import LABELS" src/ && echo "STALE REFERENCES ABOVE" || echo "no stale references"
PYTHONPATH=src uv run python -c "
from main import app  # noqa: F401
from services.categorization.pipeline import categorize_and_apply, load_config  # noqa: F401
from services.classify.apply import classify_and_label  # noqa: F401
from services.classify.classifier import Category, Verdict, classify  # noqa: F401
from workers.jobs.sync_last_7_days import sync_last_7_days  # noqa: F401
import inspect
print(inspect.signature(classify))
print('all modules import')
"
```
Expected: `no stale references`, then the signature, then `all modules import`.

- [ ] **Step 6: Verify the prompt is built from the taxonomy**

Run:
```bash
PYTHONPATH=src uv run python -c "
from services.classify.classifier import Category, _system_prompt
cats = [Category(key='to_do', display_name='Client work', description='Anything from a paying client.')]
p = _system_prompt(cats, 'I am a freelance designer.')
assert 'Client work' in p and 'paying client' in p and 'freelance designer' in p, p
assert 'confidence' in p, p
print('prompt built from taxonomy + extra instructions')
"
```
Expected: `prompt built from taxonomy + extra instructions`

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/services/categorization/pipeline.py src/services/classify/ src/workers/jobs/sync_last_7_days.py
git add src/services/categorization/pipeline.py src/services/classify/ src/workers/jobs/sync_last_7_days.py
git commit -m "feat: classify against the user's DB-backed taxonomy"
```

---

### Task 5: Shared backfill helper, re-classify job and endpoint

**Files:**
- Create: `src/services/categorization/backfill.py`, `src/workers/jobs/reclassify.py`
- Modify: `src/workers/jobs/sync_last_7_days.py:60-77`, `src/api/v1/categorization.py`, `src/schemas/categorization.py`

**Interfaces:**
- Consumes: `pipeline.get_config` (Task 4), `gmail.fetch_recent_emails`, `gmail_ops.resolve_label_id`
- Produces: `backfill.BACKFILL_CLASSIFY_MAX`, `backfill.queue_unlabelled(user_id, emails, label_names, limit) -> int`; Celery task `categorization.reclassify`; route `POST /v1/categorization/reclassify`; schemas `ReclassifyRequest`, `ReclassifyResponse`

- [ ] **Step 1: Write `src/services/categorization/backfill.py`**

```python
"""Enqueue classification for mail that does not yet carry one of our labels.

Extracted from `workers.jobs.sync_last_7_days` so onboarding and the on-demand
re-classify share one implementation instead of two that drift.
"""

from core.logging import get_logger
from services.mailman import gmail_ops
from workers.jobs.classify_new_email import classify_new_email

log = get_logger(__name__)

# Ceiling on how much mail one backfill will classify. The fetch itself may
# return far more (see gmail.FETCH_ALL_CAP); classifying all of it would mean
# thousands of LLM calls. Newest mail is the mail that matters.
BACKFILL_CLASSIFY_MAX = 200


def queue_unlabelled(
    user_id: str,
    emails: list,
    label_names: list[str],
    limit: int = BACKFILL_CLASSIFY_MAX,
) -> int:
    """Enqueue one classify task per message lacking any of `label_names`.

    Returns how many were queued. Messages already carrying one of the user's
    category labels are skipped, so re-running is cheap and never re-decides
    mail the user has already seen categorized.
    """
    known = {lid for name in label_names if (lid := gmail_ops.resolve_label_id(user_id, name))}

    queued = 0
    for email in emails:
        if queued >= limit:
            break
        if not email.id or known.intersection(email.labels or []):
            continue
        classify_new_email.delay(
            user_id,
            email.id,
            sender=email.sender,
            subject=email.subject,
            snippet=email.snippet,
        )
        queued += 1
    return queued
```

- [ ] **Step 2: Point `sync_last_7_days` at the shared helper**

In `src/workers/jobs/sync_last_7_days.py`, delete the whole `_queue_backfill_classification` function and the `BACKFILL_CLASSIFY_MAX` constant. That leaves three imports unused — `gmail_ops`, `classify_new_email`, and the `BUILTIN_CATEGORIES` line Task 4 added — so delete all three. Add:

```python
from services.categorization import backfill
from services.categorization.pipeline import get_config
```

Then replace the call site:

```python
    queued = _queue_backfill_classification(user_id, emails)
```

with:

```python
    config = get_config(user_id)
    queued = backfill.queue_unlabelled(
        user_id, emails, [c.gmail_label for c in config.categories]
    )
```

This is a behaviour improvement, not just a move: onboarding now skips mail carrying any of the user's *actual* labels, including custom ones, rather than only the six built-in names.

- [ ] **Step 3: Write `src/workers/jobs/reclassify.py`**

```python
"""Celery task: re-run categorization over a window of recent mail.

Triggered from the Categorization page after a user edits their taxonomy.
Already-categorized mail is skipped, so this is cheap to re-run and never
rewrites a decision the user has already seen.
"""

from core.logging import get_logger
from integrations.composio import gmail
from services.categorization import backfill
from services.categorization.pipeline import get_config
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="categorization.reclassify")
def reclassify(user_id: str, days: int = 7, max_results: int | None = None) -> dict:
    config = get_config(user_id)
    if not config.is_enabled:
        log.info("reclassify.disabled", user_id=user_id)
        return {"user_id": user_id, "queued": 0, "skipped_reason": "disabled"}

    emails = gmail.fetch_recent_emails(user_id, days=days, max_results=max_results)
    queued = backfill.queue_unlabelled(
        user_id, emails, [c.gmail_label for c in config.categories]
    )

    log.info("reclassify.queued", user_id=user_id, days=days, fetched=len(emails), queued=queued)
    return {"user_id": user_id, "fetched": len(emails), "queued": queued}
```

- [ ] **Step 4: Register the task and the models with the worker**

In `src/worker.py`, add to the `TASK_MODULES` list, after `"workers.jobs.sync_last_7_days"`:

```python
    "workers.jobs.reclassify",
```

The same file imports model modules so SQLAlchemy can resolve cross-model foreign keys inside worker tasks. `EmailCategory` has an FK to `users`, so add to that block (it is alphabetical, so between `auth` and `mailman`):

```python
from models import categorization as _categorization_models  # noqa: F401,E402
```

- [ ] **Step 5: Add the request/response schemas**

Append to `src/schemas/categorization.py`:

```python
class ReclassifyRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    max_results: int | None = Field(default=None, ge=1, le=2000)


class ReclassifyResponse(BaseModel):
    task_id: str
    days: int
    max_results: int | None = None
```

- [ ] **Step 6: Add the endpoint**

In `src/api/v1/categorization.py`, extend the imports:

```python
from datetime import UTC, datetime

from fastapi import status

from schemas.categorization import ReclassifyRequest, ReclassifyResponse
from workers.jobs.reclassify import reclassify as reclassify_task
```

and append the route:

```python
@router.post(
    "/reclassify",
    response_model=ReclassifyResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reclassify(
    payload: ReclassifyRequest, user: CurrentUser, db: DbSession
) -> ReclassifyResponse:
    """Queue a re-run of categorization over recent mail.

    Mail that already carries one of the user's category labels is left alone,
    so this is safe to call repeatedly after taxonomy edits.
    """
    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.is_enabled:
        raise HTTPException(409, "categorization is disabled; enable it first")

    task = reclassify_task.delay(str(user.id), payload.days, payload.max_results)
    settings_row.last_reclassify_at = datetime.now(UTC)

    return ReclassifyResponse(
        task_id=task.id, days=payload.days, max_results=payload.max_results
    )
```

- [ ] **Step 7: Verify the route is mounted and the task is registered**

Run:
```bash
PYTHONPATH=src uv run python -c "
from main import app
# app.routes is NOT scannable on FastAPI 0.139 (lazy _IncludedRouter). Use OpenAPI.
spec = app.openapi()
assert '/v1/categorization/reclassify' in spec['paths'], sorted(p for p in spec['paths'] if 'categor' in p)
assert 'post' in spec['paths']['/v1/categorization/reclassify']
from workers.jobs.reclassify import reclassify
assert reclassify.name == 'categorization.reclassify', reclassify.name
from services.categorization.backfill import queue_unlabelled, BACKFILL_CLASSIFY_MAX
assert BACKFILL_CLASSIFY_MAX == 200
print('reclassify route + task registered')
"
grep -rn "_queue_backfill_classification" src/ && echo "STALE HELPER STILL REFERENCED" || echo "old helper fully removed"
```
Expected: `reclassify route + task registered`, then `old helper fully removed`.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check src/services/categorization/ src/workers/jobs/reclassify.py src/workers/jobs/sync_last_7_days.py src/api/v1/categorization.py src/schemas/categorization.py src/worker.py
git add src/services/categorization/ src/workers/jobs/ src/api/v1/categorization.py src/schemas/categorization.py src/worker.py
git commit -m "feat: on-demand reclassify job and endpoint"
```

- [ ] **Step 9: Smoke check of Phase 1 (no Gmail writes)**

The stack is already running. Mint a token per the Global Constraints, then:

```bash
TOKEN=$(docker compose exec -T api python -c "from core.security import create_access_token; print(create_access_token('e397bee9-17ed-40d1-a3a0-0b55e115dc90'))" | tr -d '\r')
BASE=http://localhost:8000/v1/categorization

curl -s -H "Authorization: Bearer $TOKEN" $BASE/categories | python -m json.tool
```

**Do not call `POST /reclassify` with categorization enabled** — it would enqueue real LLM classification over a real inbox. Its disabled-path `409` is checked below; the happy path is verified by the registration assertions in Step 7.
Expected: six categories, `to_do` first, all `is_builtin: true`, `is_enabled: true`, `actions` all false.

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"display_name":"Action needed","is_enabled":false}' $BASE/categories/to_do | python -m json.tool
```
Expected: `display_name` is `"Action needed"`, `is_enabled` is `false`, and **`gmail_label` is still `"to do"`** — that is the invariant this whole design rests on.

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"display_name":"To do","is_enabled":true}' $BASE/categories/to_do > /dev/null
curl -s -i -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"is_enabled":false}' $BASE/categories/nope | head -1
```
Expected: `HTTP/1.1 404 Not Found` (and `to_do` restored).

Then check the reclassify guard, which reaches no external service:

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"is_enabled":false}' $BASE/settings > /dev/null
curl -s -i -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"days":7}' $BASE/reclassify | head -1
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"is_enabled":true}' $BASE/settings > /dev/null
```
Expected: `HTTP/1.1 409 Conflict`, with categorization left enabled afterwards.

---

# Phase 2 — Advanced tab

---

### Task 6: Custom categories

**Files:**
- Modify: `src/schemas/categorization.py`, `src/api/v1/categorization.py`, `src/services/categorization/store.py`

**Interfaces:**
- Consumes: `gmail.RESERVED_LABEL_NAMES`, `gmail.create_label` (Task 2 / existing)
- Produces: `store.slugify(name) -> str`, `store.delete_category(db, user_id, category)`; schema `CategoryCreate`; routes `POST /v1/categorization/categories`, `DELETE /v1/categorization/categories/{key}`

- [ ] **Step 1: Add the slug helper and cascade delete to `store.py`**

Append to `src/services/categorization/store.py`, adding `re` and the `CategorizationRule` import at the top:

```python
def slugify(display_name: str) -> str:
    """Derive a stable key from a display name: 'Client work' -> 'client_work'."""
    return re.sub(r"[^a-z0-9]+", "_", display_name.strip().casefold()).strip("_")


async def delete_category(
    db: AsyncSession, user_id: uuid.UUID, category: EmailCategory
) -> None:
    """Remove a custom category and everything that pointed at it.

    The Gmail label is deliberately left in place: nothing is stripped from the
    user's mail, so this is non-destructive and undoable by hand.
    """
    await db.execute(
        delete(CategorizationRule).where(
            CategorizationRule.user_id == user_id,
            CategorizationRule.category_key == category.key,
        )
    )
    settings_row = await get_or_create_settings(db, user_id)
    if settings_row.fallback_category_key == category.key:
        settings_row.fallback_category_key = None

    await db.delete(category)
    await db.flush()
```

Update the imports at the top of the file:

```python
import re
import uuid

from sqlalchemy import delete, select
...
from models.categorization import (
    BUILTIN_CATEGORIES,
    CategorizationRule,
    CategorizationSettings,
    EmailCategory,
    default_actions,
)
```

- [ ] **Step 2: Add `CategoryCreate` to `src/schemas/categorization.py`**

```python
class CategoryCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    color_bg: str = Field(default="#999999", pattern=HEX_COLOR)
    color_text: str = Field(default="#ffffff", pattern=HEX_COLOR)
    sort_order: int = Field(default=100, ge=0)
    actions: CategoryActions = CategoryActions()
```

- [ ] **Step 3: Add the two routes to `src/api/v1/categorization.py`**

Extend the imports:

```python
from fastapi.concurrency import run_in_threadpool

from integrations.composio import gmail
from schemas.categorization import CategoryCreate
from services.categorization.store import delete_category, slugify
```

Add the routes:

```python
@router.post(
    "/categories", response_model=CategoryRead, status_code=status.HTTP_201_CREATED
)
async def create_category(
    payload: CategoryCreate, user: CurrentUser, db: DbSession
) -> EmailCategory:
    """Create a custom category and its Gmail label.

    The Gmail label is created before the row is committed: a category whose
    label does not exist would fail every classification that picked it, so the
    Composio failure has to surface here rather than in a worker.
    """
    existing = await get_or_create_categories(db, user.id)

    gmail_label = payload.display_name.strip().casefold()
    key = slugify(payload.display_name)
    if not key:
        raise HTTPException(422, "display_name must contain at least one letter or digit")
    if gmail_label in gmail.RESERVED_LABEL_NAMES:
        raise HTTPException(422, f"{payload.display_name!r} is a reserved label name")
    if any(c.key == key for c in existing):
        raise HTTPException(422, f"a category with key {key!r} already exists")
    if any(c.gmail_label == gmail_label for c in existing):
        raise HTTPException(422, f"a category already uses the label {gmail_label!r}")

    try:
        await run_in_threadpool(gmail.create_label, str(user.id), gmail_label)
    except Exception as exc:
        raise HTTPException(502, f"could not create the Gmail label: {exc}") from exc

    category = EmailCategory(
        user_id=user.id,
        key=key,
        gmail_label=gmail_label,
        display_name=payload.display_name.strip(),
        description=payload.description,
        color_bg=payload.color_bg,
        color_text=payload.color_text,
        is_builtin=False,
        is_enabled=True,
        sort_order=payload.sort_order,
        actions=payload.actions.model_dump(),
    )
    db.add(category)
    await db.flush()
    return category


@router.delete("/categories/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_category(key: str, user: CurrentUser, db: DbSession) -> None:
    """Delete a custom category. Its Gmail label is left in the user's mailbox."""
    await get_or_create_categories(db, user.id)

    category = await get_category(db, user.id, key)
    if category is None:
        raise HTTPException(404, f"no category with key {key!r}")
    if category.is_builtin:
        raise HTTPException(409, "built-in categories cannot be deleted; disable it instead")

    await delete_category(db, user.id, category)
```

- [ ] **Step 4: Verify slug derivation and route mounting**

Run:
```bash
PYTHONPATH=src uv run python -c "
from services.categorization.store import slugify
cases = {'Client work': 'client_work', '  Invoices!  ': 'invoices', 'A/B tests': 'a_b_tests', 'FYI': 'fyi'}
for given, want in cases.items():
    got = slugify(given)
    assert got == want, f'{given!r} -> {got!r}, wanted {want!r}'
print('slugify ok')
from main import app
# app.routes is NOT scannable on FastAPI 0.139 (lazy _IncludedRouter). Use OpenAPI.
paths = app.openapi()['paths']
assert 'post' in paths['/v1/categorization/categories'], paths['/v1/categorization/categories']
assert 'delete' in paths['/v1/categorization/categories/{key}'], paths['/v1/categorization/categories/{key}']
print('create/delete routes mounted')
"
```
Expected: `slugify ok` then `create/delete routes mounted`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/services/categorization/store.py src/schemas/categorization.py src/api/v1/categorization.py
git add src/services/categorization/store.py src/schemas/categorization.py src/api/v1/categorization.py
git commit -m "feat: custom categorization categories"
```

---

### Task 7: Rules CRUD

**Files:**
- Modify: `src/services/categorization/store.py`, `src/schemas/categorization.py`, `src/api/v1/categorization.py`

**Interfaces:**
- Produces: `store.list_rules(db, user_id) -> list[CategorizationRule]`, `store.get_rule(db, user_id, rule_id) -> CategorizationRule | None`, `store.next_rule_priority(db, user_id) -> int`; schemas `RuleRead`, `RuleCreate`, `RuleUpdate`, `RuleReorder`; routes `GET/POST /v1/categorization/rules`, `PATCH/DELETE /v1/categorization/rules/{rule_id}`, `PUT /v1/categorization/rules/order`

- [ ] **Step 1: Add rule queries to `store.py`**

```python
async def list_rules(db: AsyncSession, user_id: uuid.UUID) -> list[CategorizationRule]:
    return list(
        await db.scalars(
            select(CategorizationRule)
            .where(CategorizationRule.user_id == user_id)
            .order_by(CategorizationRule.priority, CategorizationRule.created_at)
        )
    )


async def get_rule(
    db: AsyncSession, user_id: uuid.UUID, rule_id: uuid.UUID
) -> CategorizationRule | None:
    return await db.scalar(
        select(CategorizationRule).where(
            CategorizationRule.user_id == user_id, CategorizationRule.id == rule_id
        )
    )


async def next_rule_priority(db: AsyncSession, user_id: uuid.UUID) -> int:
    """One past the current maximum, so new rules land at the end."""
    highest = await db.scalar(
        select(func.max(CategorizationRule.priority)).where(
            CategorizationRule.user_id == user_id
        )
    )
    return 0 if highest is None else highest + 1
```

Add `func` to the SQLAlchemy import: `from sqlalchemy import delete, func, select`.

- [ ] **Step 2: Add rule schemas to `src/schemas/categorization.py`**

Add `import uuid` and `from pydantic import field_validator` at the top, then:

```python
class RuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_enabled: bool
    priority: int
    match_type: str
    match_value: str
    action: str
    category_key: str | None = None


class RuleCreate(BaseModel):
    """`body_keyword` matches the message *snippet*, not the full body — the
    Gmail trigger payload carries only a preview and we never re-fetch."""

    match_type: str
    match_value: str = Field(min_length=1, max_length=320)
    action: str = RULE_ASSIGN
    category_key: str | None = None
    is_enabled: bool = True

    @field_validator("match_type")
    @classmethod
    def _known_match_type(cls, value: str) -> str:
        if value not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(MATCH_TYPES)}")
        return value

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        if value not in RULE_ACTIONS:
            raise ValueError(f"action must be one of {sorted(RULE_ACTIONS)}")
        return value


class RuleUpdate(BaseModel):
    match_type: str | None = None
    match_value: str | None = Field(default=None, min_length=1, max_length=320)
    action: str | None = None
    category_key: str | None = None
    is_enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0)

    @field_validator("match_type")
    @classmethod
    def _known_match_type(cls, value: str | None) -> str | None:
        if value is not None and value not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(MATCH_TYPES)}")
        return value

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str | None) -> str | None:
        if value is not None and value not in RULE_ACTIONS:
            raise ValueError(f"action must be one of {sorted(RULE_ACTIONS)}")
        return value


class RuleReorder(BaseModel):
    rule_ids: list[uuid.UUID] = Field(min_length=1)
```

Add to the schema module's imports:

```python
from models.categorization import MATCH_TYPES, RULE_ACTIONS, RULE_ASSIGN
```

- [ ] **Step 3: Add the rules routes to `src/api/v1/categorization.py`**

Extend the imports:

```python
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from models.categorization import RULE_ASSIGN, CategorizationRule
from schemas.categorization import RuleCreate, RuleRead, RuleReorder, RuleUpdate
from services.categorization.store import get_rule, list_rules, next_rule_priority
```

`_check_rule_target` is a plain helper, not a route, so it annotates `AsyncSession` directly rather than the `DbSession` dependency alias.

Add a shared validator and the routes:

```python
async def _check_rule_target(
    db: AsyncSession, user_id: uuid.UUID, action: str, category_key: str | None
) -> None:
    """An `assign` rule must name a category that exists in this user's taxonomy."""
    if action != RULE_ASSIGN:
        return
    if not category_key:
        raise HTTPException(422, "category_key is required when action is 'assign'")
    if await get_category(db, user_id, category_key) is None:
        raise HTTPException(422, f"no category with key {category_key!r}")


@router.get("/rules", response_model=list[RuleRead])
async def get_rules(user: CurrentUser, db: DbSession) -> list[CategorizationRule]:
    """Rules in evaluation order — lowest priority first, first match wins."""
    return await list_rules(db, user.id)


@router.post("/rules", response_model=RuleRead, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleCreate, user: CurrentUser, db: DbSession
) -> CategorizationRule:
    await get_or_create_categories(db, user.id)
    await _check_rule_target(db, user.id, payload.action, payload.category_key)

    rule = CategorizationRule(
        user_id=user.id,
        priority=await next_rule_priority(db, user.id),
        match_type=payload.match_type,
        match_value=payload.match_value.strip(),
        action=payload.action,
        category_key=payload.category_key,
        is_enabled=payload.is_enabled,
    )
    db.add(rule)
    await db.flush()
    return rule


@router.patch("/rules/{rule_id}", response_model=RuleRead)
async def update_rule(
    rule_id: uuid.UUID, payload: RuleUpdate, user: CurrentUser, db: DbSession
) -> CategorizationRule:
    rule = await get_rule(db, user.id, rule_id)
    if rule is None:
        raise HTTPException(404, f"no rule with id {rule_id}")

    data = payload.model_dump(exclude_unset=True)
    # Validate the post-update state, not the patch: changing only `action` to
    # 'assign' must still be checked against the rule's existing category_key.
    action = data.get("action", rule.action)
    category_key = data.get("category_key", rule.category_key)
    await _check_rule_target(db, user.id, action, category_key)

    for field, value in data.items():
        setattr(rule, field, value.strip() if field == "match_value" else value)
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_rule(rule_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    rule = await get_rule(db, user.id, rule_id)
    if rule is None:
        raise HTTPException(404, f"no rule with id {rule_id}")
    await db.delete(rule)


@router.put("/rules/order", response_model=list[RuleRead])
async def reorder_rules(
    payload: RuleReorder, user: CurrentUser, db: DbSession
) -> list[CategorizationRule]:
    """Rewrite priorities to match the given order. Must list every rule exactly once."""
    rules = await list_rules(db, user.id)
    by_id = {rule.id: rule for rule in rules}

    if len(payload.rule_ids) != len(set(payload.rule_ids)):
        raise HTTPException(422, "rule_ids contains duplicates")
    if set(payload.rule_ids) != set(by_id):
        raise HTTPException(422, "rule_ids must list every one of your rules exactly once")

    for position, rule_id in enumerate(payload.rule_ids):
        by_id[rule_id].priority = position
    return [by_id[rule_id] for rule_id in payload.rule_ids]
```

**Route order matters:** `PUT /rules/order` must be declared before any `/rules/{rule_id}` route that also accepts `PUT`. It is not — `{rule_id}` only takes `PATCH` and `DELETE` — so declaring it last as written above is safe. Keep it that way.

- [ ] **Step 4: Verify the routes and validators**

Run:
```bash
PYTHONPATH=src uv run python -c "
from main import app
# app.routes is NOT scannable on FastAPI 0.139 (lazy _IncludedRouter). Use OpenAPI.
paths = app.openapi()['paths']
for path, method in [('/v1/categorization/rules','get'), ('/v1/categorization/rules','post'),
                     ('/v1/categorization/rules/{rule_id}','patch'),
                     ('/v1/categorization/rules/{rule_id}','delete'),
                     ('/v1/categorization/rules/order','put')]:
    assert method in paths.get(path, {}), f'missing {method.upper()} {path}'
print('rules routes mounted')

from pydantic import ValidationError
from schemas.categorization import RuleCreate
RuleCreate(match_type='sender_domain', match_value='@acme.com', action='assign', category_key='to_do')
for bad in [dict(match_type='nope', match_value='x'), dict(match_type='sender_domain', match_value='x', action='nope')]:
    try:
        RuleCreate(**bad); raise SystemExit(f'should have rejected {bad}')
    except ValidationError:
        pass
print('rule schema validation ok')
"
```
Expected: `rules routes mounted` then `rule schema validation ok`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/services/categorization/store.py src/schemas/categorization.py src/api/v1/categorization.py
git add src/services/categorization/store.py src/schemas/categorization.py src/api/v1/categorization.py
git commit -m "feat: categorization rules CRUD and reordering"
```

---

### Task 8: The rules pass in the pipeline

**Files:**
- Create: `src/services/categorization/rules.py`
- Modify: `src/services/categorization/pipeline.py`

**Interfaces:**
- Consumes: `models.categorization` match constants
- Produces: `rules.RuleSnapshot`, `rules.matches(rule, sender, subject, snippet) -> bool`, `rules.first_match(rules, sender, subject, snippet) -> RuleSnapshot | None`; `UserConfig.rules`

- [ ] **Step 1: Write `src/services/categorization/rules.py`**

```python
"""Deterministic rule matching. Pure functions — no DB, no network, no LLM.

Kept separate from the pipeline because this is where the fiddly semantics live
(address parsing, domain normalisation, case folding) and it is the one part of
categorization that can be reasoned about in isolation.
"""

from dataclasses import dataclass
from email.utils import parseaddr

from models.categorization import (
    MATCH_BODY_KEYWORD,
    MATCH_SENDER_ADDRESS,
    MATCH_SENDER_DOMAIN,
    MATCH_SUBJECT_KEYWORD,
)


@dataclass(frozen=True)
class RuleSnapshot:
    """A rule detached from the DB session."""

    match_type: str
    match_value: str
    action: str
    category_key: str | None


def _address(sender: str | None) -> str:
    """The bare address out of a From header: 'Bo <bo@acme.com>' -> 'bo@acme.com'."""
    return parseaddr(sender or "")[1].strip().casefold()


def _domain(sender: str | None) -> str:
    address = _address(sender)
    return address.rpartition("@")[2] if "@" in address else ""


def matches(
    rule: RuleSnapshot, sender: str | None, subject: str | None, snippet: str | None
) -> bool:
    value = rule.match_value.strip().casefold()
    if not value:
        return False

    if rule.match_type == MATCH_SENDER_ADDRESS:
        return _address(sender) == value
    if rule.match_type == MATCH_SENDER_DOMAIN:
        # Accept the value with or without a leading '@'.
        return _domain(sender) == value.lstrip("@")
    if rule.match_type == MATCH_SUBJECT_KEYWORD:
        return value in (subject or "").casefold()
    if rule.match_type == MATCH_BODY_KEYWORD:
        # The snippet, not the full body — the trigger payload has no body.
        return value in (snippet or "").casefold()
    return False


def first_match(
    rules: list[RuleSnapshot],
    sender: str | None,
    subject: str | None,
    snippet: str | None,
) -> RuleSnapshot | None:
    """First matching rule wins. `rules` must already be in priority order."""
    for rule in rules:
        if matches(rule, sender, subject, snippet):
            return rule
    return None
```

- [ ] **Step 2: Carry rules through `UserConfig` in `pipeline.py`**

Add the imports:

```python
from models.categorization import RULE_EXCLUDE
from services.categorization.rules import RuleSnapshot, first_match
from services.categorization.store import list_rules
```

Add the field to `UserConfig`:

```python
@dataclass(frozen=True)
class UserConfig:
    is_enabled: bool
    categories: tuple[CategorySnapshot, ...]
    rules: tuple[RuleSnapshot, ...] = ()
```

and populate it in `load_config`, before the `return`:

```python
    rule_rows = await list_rules(db, user_id)
```

then inside the returned `UserConfig(...)`:

```python
        rules=tuple(
            RuleSnapshot(
                match_type=r.match_type,
                match_value=r.match_value,
                action=r.action,
                category_key=r.category_key,
            )
            for r in rule_rows
            if r.is_enabled
        ),
```

- [ ] **Step 3: Run the rules pass before the LLM in `categorize_and_apply`**

Replace the block that starts at `enabled = config.enabled()` and ends at `if category is None: return None` with:

```python
    enabled = config.enabled()
    if not enabled:
        log.info("categorize.no_categories", user_id=user_id, message_id=message_id)
        return None

    # Deterministic rules first: a match here means no LLM call at all.
    rule = first_match(list(config.rules), sender, subject, snippet)
    if rule is not None:
        if rule.action == RULE_EXCLUDE:
            log.info("categorize.excluded", user_id=user_id, message_id=message_id)
            return None
        category = config.by_key(rule.category_key or "")
        if category is None:
            log.warning(
                "categorize.rule_target_missing",
                user_id=user_id,
                category_key=rule.category_key,
            )
            return None
    else:
        verdict = classify(
            sender,
            subject,
            snippet,
            categories=[
                Category(key=c.key, display_name=c.display_name, description=c.description)
                for c in enabled
            ],
        )
        if verdict.key is None:
            return None
        category = config.by_key(verdict.key)
        if category is None:
            return None
```

The `gmail_ops.add_label(...)` call and the `return category.key` below it stay as they are. Change the log line's `confidence=verdict.confidence` to `matched_rule=rule is not None`, since `verdict` no longer exists on the rule branch.

A rule may target a disabled category — that is intentional. An explicit rule is a stronger signal than the enable/disable toggle, which only governs what the LLM may choose from.

- [ ] **Step 4: Verify the matching semantics**

Run:
```bash
PYTHONPATH=src uv run python -c "
from services.categorization.rules import RuleSnapshot, matches, first_match

def r(t, v, action='assign', key='to_do'):
    return RuleSnapshot(match_type=t, match_value=v, action=action, category_key=key)

FROM = 'Bo Diddley <Bo@Acme.COM>'
assert matches(r('sender_address', 'bo@acme.com'), FROM, None, None)
assert matches(r('sender_address', 'BO@ACME.COM'), FROM, None, None)
assert not matches(r('sender_address', 'acme.com'), FROM, None, None)
assert matches(r('sender_domain', 'acme.com'), FROM, None, None)
assert matches(r('sender_domain', '@acme.com'), FROM, None, None)
assert not matches(r('sender_domain', 'notacme.com'), FROM, None, None)
assert matches(r('subject_keyword', 'INVOICE'), None, 'Your invoice is ready', None)
assert not matches(r('subject_keyword', 'invoice'), None, 'Your bill is ready', None)
assert matches(r('body_keyword', 'unsubscribe'), None, None, 'Click UNSUBSCRIBE below')
assert not matches(r('sender_domain', '   '), FROM, None, None)
assert not matches(r('bogus_type', 'x'), FROM, None, None)

ordered = [r('sender_domain', 'acme.com', 'exclude', None), r('sender_address', 'bo@acme.com')]
assert first_match(ordered, FROM, None, None).action == 'exclude', 'priority order not respected'
assert first_match([r('subject_keyword', 'zzz')], FROM, 'nope', None) is None
print('rule matching ok')
"
PYTHONPATH=src uv run python -c "
from services.categorization.pipeline import UserConfig, load_config, categorize_and_apply  # noqa: F401
print('pipeline imports with rules')
"
```
Expected: `rule matching ok` then `pipeline imports with rules`.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/services/categorization/rules.py src/services/categorization/pipeline.py
git add src/services/categorization/rules.py src/services/categorization/pipeline.py
git commit -m "feat: deterministic rules bypass the classifier"
```

---

### Task 9: Classifier tuning knobs

**Files:**
- Modify: `src/core/config.py`, `src/schemas/categorization.py`, `src/api/v1/categorization.py`, `src/services/categorization/pipeline.py`

**Interfaces:**
- Consumes: `classifier.Verdict.confidence` (Task 4)
- Produces: `settings.CLASSIFIER_MODELS`, `settings.allowed_classifier_models` property; widened `SettingsRead`/`SettingsUpdate`; `UserConfig.fallback_category_key` / `.confidence_threshold` / `.model` / `.extra_instructions`

- [ ] **Step 1: Add the model allowlist to `src/core/config.py`**

Next to `OPENAI_MODEL` at line 50:

```python
    # Models a user may pick for their classifier. OPENAI_MODEL is always
    # allowed on top of this, so changing the default cannot invalidate a
    # per-user choice that is already stored.
    CLASSIFIER_MODELS: str = "gpt-4o-mini,gpt-4o"
```

`Settings` currently has no properties — it is a flat field list ending at `RECALL_TIMEOUT_SECONDS: float = 30.0`. Add this as the first one, after that last field and before the class body ends:

```python
    @property
    def allowed_classifier_models(self) -> set[str]:
        listed = {m.strip() for m in self.CLASSIFIER_MODELS.split(",") if m.strip()}
        return listed | {self.OPENAI_MODEL}
```

Also add the variable to `.env.example`, under the existing `OPENAI_API_KEY` line:

```
CLASSIFIER_MODELS=gpt-4o-mini,gpt-4o
```

- [ ] **Step 2: Widen the settings schemas in `src/schemas/categorization.py`**

Replace `SettingsRead` and `SettingsUpdate` with:

```python
class SettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    is_enabled: bool
    fallback_category_key: str | None = None
    confidence_threshold: float = 0.0
    model: str | None = None
    extra_instructions: str | None = None
    last_reclassify_at: datetime | None = None


class SettingsUpdate(BaseModel):
    is_enabled: bool | None = None
    fallback_category_key: str | None = None
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    model: str | None = None
    extra_instructions: str | None = Field(default=None, max_length=2000)
```

`model_config` is Pydantic's own attribute name, and a field called `model` is
adjacent to it but does not collide — Pydantic v2 only reserves the `model_`
prefix followed by more characters. No warning is emitted for a bare `model`
field. If a `protected_namespaces` warning does appear, add
`model_config = ConfigDict(protected_namespaces=())` to `SettingsUpdate`.

- [ ] **Step 3: Validate the new fields in `update_settings`**

Replace the body of `update_settings` in `src/api/v1/categorization.py`:

```python
@router.put("/settings", response_model=SettingsRead)
async def update_settings(
    payload: SettingsUpdate, user: CurrentUser, db: DbSession
) -> CategorizationSettings:
    row = await get_or_create_settings(db, user.id)
    data = payload.model_dump(exclude_unset=True)

    if (model := data.get("model")) is not None:
        allowed = app_settings.allowed_classifier_models
        if model not in allowed:
            raise HTTPException(422, f"model must be one of {sorted(allowed)}")

    if "fallback_category_key" in data and data["fallback_category_key"] is not None:
        await get_or_create_categories(db, user.id)
        if await get_category(db, user.id, data["fallback_category_key"]) is None:
            raise HTTPException(422, f"no category with key {data['fallback_category_key']!r}")

    for field, value in data.items():
        setattr(row, field, value)
    return row
```

Add the import:

```python
from core.config import settings as app_settings
```

- [ ] **Step 4: Apply the knobs in `pipeline.py`**

Widen `UserConfig`:

```python
@dataclass(frozen=True)
class UserConfig:
    is_enabled: bool
    categories: tuple[CategorySnapshot, ...]
    rules: tuple[RuleSnapshot, ...] = ()
    fallback_category_key: str | None = None
    confidence_threshold: float = 0.0
    model: str | None = None
    extra_instructions: str | None = None
```

Populate the four new fields in `load_config` from `settings_row`:

```python
        fallback_category_key=settings_row.fallback_category_key,
        confidence_threshold=settings_row.confidence_threshold,
        model=settings_row.model,
        extra_instructions=settings_row.extra_instructions,
```

In `categorize_and_apply`, replace the `else:` branch from Task 8 with:

```python
    else:
        verdict = classify(
            sender,
            subject,
            snippet,
            categories=[
                Category(key=c.key, display_name=c.display_name, description=c.description)
                for c in enabled
            ],
            model=config.model,
            extra_instructions=config.extra_instructions,
        )
        key = verdict.key
        if key is None or verdict.confidence < config.confidence_threshold:
            # Undecided, or the model was not sure enough to be trusted.
            log.info(
                "categorize.below_threshold",
                user_id=user_id,
                message_id=message_id,
                key=key,
                confidence=verdict.confidence,
                threshold=config.confidence_threshold,
            )
            key = config.fallback_category_key
        if key is None:
            return None
        category = config.by_key(key)
        if category is None:
            return None
```

- [ ] **Step 5: Verify the allowlist and the threshold logic**

Run:
```bash
PYTHONPATH=src uv run python -c "
from core.config import settings
allowed = settings.allowed_classifier_models
assert 'gpt-4o-mini' in allowed and 'gpt-4o' in allowed, allowed
assert settings.OPENAI_MODEL in allowed, allowed
print('model allowlist:', sorted(allowed))

from schemas.categorization import SettingsUpdate
from pydantic import ValidationError
SettingsUpdate(confidence_threshold=0.0); SettingsUpdate(confidence_threshold=1.0)
for bad in (-0.1, 1.1):
    try:
        SettingsUpdate(confidence_threshold=bad); raise SystemExit(f'accepted {bad}')
    except ValidationError:
        pass
print('threshold bounds enforced')

from services.categorization.pipeline import UserConfig
c = UserConfig(is_enabled=True, categories=(), confidence_threshold=0.8, model='gpt-4o')
assert c.confidence_threshold == 0.8 and c.model == 'gpt-4o'
print('UserConfig carries the knobs')
"
```
Expected: the allowlist, then `threshold bounds enforced`, then `UserConfig carries the knobs`.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/core/config.py src/schemas/categorization.py src/api/v1/categorization.py src/services/categorization/pipeline.py
git add src/core/config.py .env.example src/schemas/categorization.py src/api/v1/categorization.py src/services/categorization/pipeline.py
git commit -m "feat: classifier tuning knobs for categorization"
```

---

### Task 10: Per-category Gmail actions

**Files:**
- Modify: `src/services/mailman/gmail_ops.py`, `src/services/categorization/pipeline.py`

**Interfaces:**
- Consumes: `gmail_ops._modify`, `gmail_ops.resolve_label_id`
- Produces: `gmail_ops.UNREAD_LABEL`, `gmail_ops.STARRED_LABEL`, `gmail_ops.apply_category(user_id, message_ids, label_name, actions)`

- [ ] **Step 1: Add the constants and `apply_category` to `src/services/mailman/gmail_ops.py`**

Next to `INBOX_LABEL` at line 12:

```python
UNREAD_LABEL = "UNREAD"
STARRED_LABEL = "STARRED"
```

And after `add_label`:

```python
def apply_category(
    user_id: str,
    message_ids: list[str],
    label_name: str,
    actions: dict[str, bool] | None = None,
) -> None:
    """Apply a category label and its side effects in one Composio round trip.

    `actions` keys are `archive`, `mark_read`, and `star` (see
    `models.categorization.CATEGORY_ACTIONS`). Batching them with the label is
    the point: four separate calls per message would quadruple the Gmail cost of
    classification.
    """
    label_id = resolve_label_id(user_id, label_name)
    if not label_id:
        raise RuntimeError(f"label {label_name!r} not found for user {user_id}")

    actions = actions or {}
    add = [label_id]
    remove = []
    if actions.get("archive"):
        remove.append(INBOX_LABEL)
    if actions.get("mark_read"):
        remove.append(UNREAD_LABEL)
    if actions.get("star"):
        add.append(STARRED_LABEL)

    _modify(user_id, message_ids, add=add, remove=remove)
```

- [ ] **Step 2: Use it in `pipeline.py`**

Replace:

```python
    gmail_ops.add_label(user_id, [message_id], category.gmail_label)
```

with:

```python
    gmail_ops.apply_category(
        user_id, [message_id], category.gmail_label, category.actions
    )
```

and add `actions=category.actions` to the `log.info("categorize.applied", ...)` call.

- [ ] **Step 3: Verify the add/remove sets**

`apply_category` is the one place a mistake silently mangles a user's inbox, so check the label-id maths directly against a stubbed Composio.

Run:
```bash
PYTHONPATH=src uv run python -c "
from unittest.mock import patch
from services.mailman import gmail_ops

calls = []
def fake_modify(user_id, message_ids, add, remove):
    calls.append((sorted(add), sorted(remove)))

with patch.object(gmail_ops, 'resolve_label_id', return_value='Label_9'), \
     patch.object(gmail_ops, '_modify', fake_modify):
    gmail_ops.apply_category('u', ['m'], 'to do', None)
    gmail_ops.apply_category('u', ['m'], 'to do', {'archive': True})
    gmail_ops.apply_category('u', ['m'], 'to do', {'mark_read': True, 'star': True})
    gmail_ops.apply_category('u', ['m'], 'to do', {'archive': True, 'mark_read': True, 'star': True})

assert calls[0] == (['Label_9'], []), calls[0]
assert calls[1] == (['Label_9'], ['INBOX']), calls[1]
assert calls[2] == (['Label_9', 'STARRED'], ['UNREAD']), calls[2]
assert calls[3] == (['Label_9', 'STARRED'], ['INBOX', 'UNREAD']), calls[3]
assert len(calls) == 4, 'one call per message, not one per action'
print('apply_category label maths ok')
"
```
Expected: `apply_category label maths ok`

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/services/mailman/gmail_ops.py src/services/categorization/pipeline.py
git add src/services/mailman/gmail_ops.py src/services/categorization/pipeline.py
git commit -m "feat: per-category gmail actions"
```

- [ ] **Step 5: Full-surface smoke check, in-process with Composio stubbed**

Creating a custom category calls Gmail for real, which the Global Constraints forbid. Exercise the whole surface in-process instead: the real routes, real validation, and the real dev database, with only `gmail.create_label` stubbed. `./src` is bind-mounted into the api container, so it already sees your code.

Write this to `/tmp/smoke.py` and run it with `docker compose exec -T api python /tmp/smoke.py` (copy it in with `docker compose cp` or a heredoc):

```python
"""Full-surface smoke check. Real routes + real DB; Composio stubbed."""

import asyncio
from unittest.mock import patch

import httpx

from core.security import create_access_token
from integrations.composio import gmail
from main import app

USER_ID = "e397bee9-17ed-40d1-a3a0-0b55e115dc90"
BASE = "/v1/categorization"
AUTH = {"Authorization": f"Bearer {create_access_token(USER_ID)}"}


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Clean slate for the custom category this script creates.
        await c.delete(f"{BASE}/categories/client_work", headers=AUTH)

        r = await c.post(
            f"{BASE}/categories",
            headers=AUTH,
            json={
                "display_name": "Client work",
                "description": "Anything from a paying client.",
                "actions": {"star": True},
            },
        )
        assert r.status_code == 201, (r.status_code, r.text)
        body = r.json()
        assert body["key"] == "client_work", body
        assert body["gmail_label"] == "client work", body
        assert body["is_builtin"] is False, body
        assert body["actions"]["star"] is True, body
        print("create custom category: ok")

        r = await c.post(
            f"{BASE}/categories",
            headers=AUTH,
            json={"display_name": "FYI", "description": "dupe"},
        )
        assert r.status_code == 422, (r.status_code, r.text)
        print("duplicate key rejected: ok")

        r = await c.post(
            f"{BASE}/rules",
            headers=AUTH,
            json={
                "match_type": "sender_domain",
                "match_value": "@acme.com",
                "action": "assign",
                "category_key": "client_work",
            },
        )
        assert r.status_code == 201, (r.status_code, r.text)
        rule_id = r.json()["id"]
        print("create rule: ok")

        r = await c.post(
            f"{BASE}/rules",
            headers=AUTH,
            json={
                "match_type": "sender_domain",
                "match_value": "@acme.com",
                "action": "assign",
                "category_key": "ghost",
            },
        )
        assert r.status_code == 422, (r.status_code, r.text)
        print("rule against unknown category rejected: ok")

        r = await c.delete(f"{BASE}/categories/to_do", headers=AUTH)
        assert r.status_code == 409, (r.status_code, r.text)
        print("builtin delete refused: ok")

        r = await c.delete(f"{BASE}/categories/client_work", headers=AUTH)
        assert r.status_code == 204, (r.status_code, r.text)

        r = await c.get(f"{BASE}/rules", headers=AUTH)
        assert all(x["id"] != rule_id for x in r.json()), r.json()
        print("category delete cascaded to its rule: ok")


with patch.object(gmail, "create_label", return_value="Label_stub"):
    asyncio.run(main())
print("\nfull surface ok")
```

Expected: every line prints `ok`, ending with `full surface ok`. If `create_label` is invoked for real, the patch failed — stop and fix the patch target rather than letting it through.

---

## Done

Twelve operations across seven paths under `/v1/categorization`:

| Method | Path |
|---|---|
| `GET`, `POST` | `/categories` |
| `PATCH`, `DELETE` | `/categories/{key}` |
| `GET`, `POST` | `/rules` |
| `PATCH`, `DELETE` | `/rules/{rule_id}` |
| `PUT` | `/rules/order` |
| `GET`, `PUT` | `/settings` |
| `POST` | `/reclassify` |
