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
from models.categorization import RULE_EXCLUDE
from services.categorization.rules import RuleSnapshot, first_match
from services.categorization.store import (
    get_or_create_categories,
    get_or_create_settings,
    list_rules,
)
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
    rules: tuple[RuleSnapshot, ...] = ()
    fallback_category_key: str | None = None
    confidence_threshold: float = 0.0
    model: str | None = None
    extra_instructions: str | None = None

    def enabled(self) -> list[CategorySnapshot]:
        return [c for c in self.categories if c.is_enabled]

    def by_key(self, key: str) -> CategorySnapshot | None:
        return next((c for c in self.categories if c.key == key), None)


async def load_config(db: AsyncSession, user_id: uuid.UUID) -> UserConfig:
    settings_row = await get_or_create_settings(db, user_id)
    categories = await get_or_create_categories(db, user_id)
    rule_rows = await list_rules(db, user_id)
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
        fallback_category_key=settings_row.fallback_category_key,
        confidence_threshold=settings_row.confidence_threshold,
        model=settings_row.model,
        extra_instructions=settings_row.extra_instructions,
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

    # Deterministic rules first: a match here means no LLM call at all. An
    # explicit rule outranks the enable toggle by design, so this must run
    # even when every category is disabled — only the LLM branch below needs
    # at least one enabled category to pick from.
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
                message_id=message_id,
                category_key=rule.category_key,
            )
            return None
    else:
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
