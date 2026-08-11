"""Per-user email categorization: taxonomy, deterministic rules, tuning knobs.

Three tables. `email_categories` is the user's taxonomy — the six built-ins are
seeded on first read and can be renamed, recoloured, disabled, or joined by
custom ones. `categorization_rules` are deterministic matches evaluated before
the LLM is ever called. `categorization_settings` is the per-user singleton of
tuning knobs.

`BUILTIN_CATEGORIES` lives here rather than in the service layer because
`integrations.google.gmail` also needs it to provision Gmail labels, and
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
