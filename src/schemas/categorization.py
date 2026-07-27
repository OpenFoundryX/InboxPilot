"""Pydantic schemas for the Categorization API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.categorization import MATCH_TYPES, RULE_ACTIONS, RULE_ASSIGN

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


class CategoryCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    color_bg: str = Field(default="#999999", pattern=HEX_COLOR)
    color_text: str = Field(default="#ffffff", pattern=HEX_COLOR)
    sort_order: int = Field(default=100, ge=0)
    actions: CategoryActions = CategoryActions()


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


class ReclassifyRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    max_results: int | None = Field(default=None, ge=1, le=2000)


class ReclassifyResponse(BaseModel):
    task_id: str
    days: int
    max_results: int | None = None


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
