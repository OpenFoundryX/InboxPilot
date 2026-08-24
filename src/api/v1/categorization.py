"""Categorization API — the user's taxonomy, rules, and classifier settings.

Backs the Categorization page. The General tab reads and edits the six built-in
categories; the Advanced tab adds custom categories, deterministic rules, and
tuning knobs.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import DbSession
from core.config import settings as app_settings
from integrations.google import gmail
from models.categorization import (
    RULE_ASSIGN,
    CategorizationRule,
    CategorizationSettings,
    EmailCategory,
    default_actions,
)
from models.users import User
from schemas.categorization import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
    ReclassifyRequest,
    ReclassifyResponse,
    RuleCreate,
    RuleRead,
    RuleReorder,
    RuleUpdate,
    SettingsRead,
    SettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.billing.dependencies import EntitledUser
from services.billing.entitlements import FEATURE_CUSTOM_CATEGORIES, REASON_LOCKED, check
from services.categorization.store import (
    delete_category,
    get_category,
    get_or_create_categories,
    get_or_create_settings,
    get_rule,
    list_rules,
    next_rule_priority,
    slugify,
)
from workers.jobs.reclassify import reclassify as reclassify_task
from workers.jobs.sync_category_inbox import sync_category_inbox

router = APIRouter(prefix="/categorization", tags=["categorization"])

CurrentUser = Annotated[User, Depends(get_current_user)]


async def _require_custom_categories(db: DbSession, user: User) -> None:
    """Reject custom-category/rule mutations the plan doesn't include.

    `entitlements.check` rather than `EntitledUser`/`require_entitled`: the
    latter only answers "is this account live" and would let a locked-out-of-
    the-feature-but-still-entitled Starter user straight through. Starter's
    taxonomy is the built-in categories only — see the pricing matrix's
    "Basic" cell and `GET /billing/plans`'s `custom_categories: false` — but
    nothing enforced it until now.
    """
    decision = await check(db, user.id, FEATURE_CUSTOM_CATEGORIES)
    if not decision.allowed:
        detail = (
            "Your subscription is not active."
            if decision.reason == REASON_LOCKED
            else "Custom categories and rules are a Pro feature — upgrade your plan to unlock them."
        )
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail)


@router.get("/categories", response_model=list[CategoryRead])
async def list_categories(user: CurrentUser, db: DbSession) -> list[EmailCategory]:
    """The user's taxonomy in display order; seeds the built-ins on first call."""
    return await get_or_create_categories(db, user.id)


@router.patch("/categories/{key}", response_model=CategoryRead)
async def update_category(
    key: str, payload: CategoryUpdate, user: CurrentUser, db: DbSession
) -> EmailCategory:

    # Seed first: a user who PATCHes before ever listing still has a taxonomy.
    existing = await get_or_create_categories(db, user.id)

    category = await get_category(db, user.id, key)
    if category is None:
        raise HTTPException(404, f"no category with key {key!r}")

    # Built-in categories stay editable on Starter (enable/disable, archive).
    # Custom ones are a Pro entitlement — same gate as create.
    if not category.is_builtin:
        await _require_custom_categories(db, user)

    data = payload.model_dump(exclude_unset=True)

    if data.get("display_name") is not None:
        data["display_name"] = data["display_name"].strip()
        # Same collision guard as create: a display_name that normalizes to
        # match another category shadows it in the classifier's name->key map
        # (first-wins), silently misrouting mail forever. Exclude this row
        # itself so renaming to a case/whitespace variant of its own current
        # name still works.
        new_norm = data["display_name"].casefold()
        if any(
            c.key != category.key and c.display_name.strip().casefold() == new_norm
            for c in existing
        ):
            raise HTTPException(422, f"a category is already named {data['display_name']!r}")

    if "actions" in data:
        if data["actions"] is None:
            raise HTTPException(422, "actions cannot be null")
        # exclude_unset recurses into nested models, so a partial actions PATCH
        # (e.g. {"archive": true}) only carries the keys the caller sent. Merge
        # over the stored value (itself defaulted, in case it's ever missing a
        # key) instead of assigning outright, or the omitted keys are dropped
        # from the JSONB column rather than left at their prior value.
        data["actions"] = {**default_actions(), **(category.actions or {}), **data["actions"]}

    # The archive flag is normally applied by the classifier as each email is
    # categorized, so on its own it only affects new mail. Flipping the toggle
    # means the mailbox, not just future arrivals — so when it actually changes,
    # sweep the mail already carrying this label onto the right side of the inbox.
    was_archived = bool((category.actions or {}).get("archive"))
    now_archived = bool(data.get("actions", category.actions or {}).get("archive"))
    archive_changed = "actions" in data and was_archived != now_archived

    for field, value in data.items():
        setattr(category, field, value)

    if archive_changed:
        sync_category_inbox.delay(str(user.id), category.gmail_label, now_archived)

    return category


@router.get("/settings", response_model=SettingsRead)
async def get_settings(user: CurrentUser, db: DbSession) -> CategorizationSettings:
    return await get_or_create_settings(db, user.id)


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


@router.post(
    "/reclassify",
    response_model=ReclassifyResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reclassify(
    payload: ReclassifyRequest, user: EntitledUser, db: DbSession
) -> ReclassifyResponse:
    """Queue a re-run of categorization over recent mail.

    Mail that already carries one of the user's category labels is left alone,
    so this is safe to call repeatedly after taxonomy edits.

    Gated on `EntitledUser`: this fans out `classify_new_email` (an LLM call)
    per matched message, and each one now also chains an auto-draft attempt —
    a locked account calling this repeatedly had no cap on either.
    """
    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.is_enabled:
        raise HTTPException(409, "categorization is disabled; enable it first")

    # Stamp before enqueueing: if the broker publish in `.delay()` raises, the
    # request unwinds and the stamp rolls back with it, so the two stay
    # consistent. `task.id` is safe either way — Celery generates it
    # client-side before publishing.
    settings_row.last_reclassify_at = datetime.now(UTC)
    task = reclassify_task.delay(str(user.id), payload.days, payload.max_results)

    return ReclassifyResponse(task_id=task.id, days=payload.days, max_results=payload.max_results)


@router.post("/categories", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryCreate, user: CurrentUser, db: DbSession
) -> EmailCategory:
    """Create a custom category and its Gmail label.

    The Gmail label is created before the row is committed: a category whose
    label does not exist would fail every classification that picked it, so the
    A Gmail failure has to surface here rather than in a worker.
    """
    await _require_custom_categories(db, user)
    existing = await get_or_create_categories(db, user.id)

    gmail_label = payload.display_name.strip().casefold()
    key = slugify(payload.display_name)
    if not key:
        raise HTTPException(422, "display_name must contain at least one letter or digit")
    # casefold() can *lengthen* a string (e.g. 'ß' -> 'ss'), so a display_name
    # that fits Pydantic's max_length=64 on the input can still overflow the
    # String(64) columns once normalised. Check the derived values themselves,
    # not the input length, and do it before the irreversible Gmail call.
    if len(gmail_label) > 64 or len(key) > 64:
        raise HTTPException(422, "display_name is too long once normalised")
    if gmail_label in gmail.RESERVED_LABEL_NAMES:
        raise HTTPException(422, f"{payload.display_name!r} is a reserved label name")
    if any(c.key == key for c in existing):
        raise HTTPException(422, f"a category with key {key!r} already exists")
    if any(c.gmail_label == gmail_label for c in existing):
        raise HTTPException(422, f"a category already uses the label {gmail_label!r}")
    # Colliding display names shadow each other in the classifier: the model
    # picks a category by display name, so two categories that normalize to
    # the same string would make mail for the shadowed one silently land in
    # the other, permanently. Reject the collision outright rather than let
    # the classifier degrade.
    if any(
        c.display_name.strip().casefold() == payload.display_name.strip().casefold()
        for c in existing
    ):
        raise HTTPException(422, f"a category is already named {payload.display_name.strip()!r}")

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
    try:
        await db.flush()
    except IntegrityError as exc:
        # A concurrent request can win the uq_email_categories_user_key race
        # between our pre-checks and this flush. `create_label` is NOT
        # idempotent (it raises on Gmail's duplicate-name 409 rather than
        # returning the existing id), so the loser's Gmail label call may
        # itself have already failed or produced a duplicate label; either
        # way the loser's row here is discarded and only the winning
        # request's row remains linked to a label, so nothing is orphaned in
        # the DB even though a stray duplicate Gmail label can exist.
        raise HTTPException(409, "that category was just created by another request") from exc
    return category


@router.delete("/categories/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_category(key: str, user: CurrentUser, db: DbSession) -> None:
    """Delete a custom category. Its Gmail label is left in the user's mailbox."""
    await _require_custom_categories(db, user)
    await get_or_create_categories(db, user.id)

    category = await get_category(db, user.id, key)
    if category is None:
        raise HTTPException(404, f"no category with key {key!r}")
    if category.is_builtin:
        raise HTTPException(409, "built-in categories cannot be deleted; disable it instead")

    await delete_category(db, user.id, category)


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
async def create_rule(payload: RuleCreate, user: CurrentUser, db: DbSession) -> CategorizationRule:
    await _require_custom_categories(db, user)
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
    await _require_custom_categories(db, user)
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
    await _require_custom_categories(db, user)
    rule = await get_rule(db, user.id, rule_id)
    if rule is None:
        raise HTTPException(404, f"no rule with id {rule_id}")
    await db.delete(rule)


@router.put("/rules/order", response_model=list[RuleRead])
async def reorder_rules(
    payload: RuleReorder, user: CurrentUser, db: DbSession
) -> list[CategorizationRule]:
    """Rewrite priorities to match the given order. Must list every rule exactly once."""
    await _require_custom_categories(db, user)
    rules = await list_rules(db, user.id)
    by_id = {rule.id: rule for rule in rules}

    if len(payload.rule_ids) != len(set(payload.rule_ids)):
        raise HTTPException(422, "rule_ids contains duplicates")
    if set(payload.rule_ids) != set(by_id):
        raise HTTPException(422, "rule_ids must list every one of your rules exactly once")

    for position, rule_id in enumerate(payload.rule_ids):
        by_id[rule_id].priority = position
    return [by_id[rule_id] for rule_id in payload.rule_ids]
