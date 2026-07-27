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


class ReclassifyRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    max_results: int | None = Field(default=None, ge=1, le=2000)


class ReclassifyResponse(BaseModel):
    task_id: str
    days: int
    max_results: int | None = None
