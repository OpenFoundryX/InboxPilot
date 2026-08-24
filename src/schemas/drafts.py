"""Pydantic schemas for the Drafts API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.drafts import FILE_PURPOSES, LENGTHS, SELECTIVITY_LEVELS, TONES


class DraftSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    is_enabled: bool
    category_keys: list[str]
    selectivity: str
    tone: str
    length: str
    custom_instructions_enabled: bool
    custom_instructions: str | None
    signature_enabled: bool
    signature: str | None
    follow_up_enabled: bool
    follow_up_days: int
    model: str | None
    last_sweep_at: datetime | None
    last_follow_up_at: datetime | None


class DraftSettingsUpdate(BaseModel):
    """Partial update. Absent fields are left alone (`exclude_unset`)."""

    is_enabled: bool | None = None
    category_keys: list[str] | None = None
    selectivity: str | None = None
    tone: str | None = None
    length: str | None = None
    custom_instructions_enabled: bool | None = None
    # Explicitly nullable: clearing the textarea sends null, which must erase the
    # stored text rather than be read as "leave unchanged".
    custom_instructions: str | None = None
    signature_enabled: bool | None = None
    signature: str | None = None
    follow_up_enabled: bool | None = None
    follow_up_days: int | None = Field(default=None, ge=1, le=30)
    model: str | None = None

    @field_validator("selectivity")
    @classmethod
    def _check_selectivity(cls, value: str | None) -> str | None:
        if value is not None and value not in SELECTIVITY_LEVELS:
            raise ValueError(f"selectivity must be one of {sorted(SELECTIVITY_LEVELS)}")
        return value

    @field_validator("tone")
    @classmethod
    def _check_tone(cls, value: str | None) -> str | None:
        if value is not None and value not in TONES:
            raise ValueError(f"tone must be one of {sorted(TONES)}")
        return value

    @field_validator("length")
    @classmethod
    def _check_length(cls, value: str | None) -> str | None:
        if value is not None and value not in LENGTHS:
            raise ValueError(f"length must be one of {sorted(LENGTHS)}")
        return value

    @field_validator("category_keys")
    @classmethod
    def _dedupe_keys(cls, value: list[str] | None) -> list[str] | None:
        """Drop duplicates while keeping order — they would double-count nothing
        useful and make the UI's selection state ambiguous."""
        if value is None:
            return None
        seen: set[str] = set()
        out: list[str] = []
        for key in value:
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out


class DraftFileRead(BaseModel):
    """No `extracted_text` field: it can be hundreds of kilobytes, and the list
    view only needs to show what was uploaded and how much text came out."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purpose: str
    filename: str
    content_type: str
    size_bytes: int
    char_count: int
    is_enabled: bool
    created_at: datetime


class DraftFileUpdate(BaseModel):
    is_enabled: bool | None = None
    purpose: str | None = None

    @field_validator("purpose")
    @classmethod
    def _check_purpose(cls, value: str | None) -> str | None:
        if value is not None and value not in FILE_PURPOSES:
            raise ValueError(f"purpose must be one of {sorted(FILE_PURPOSES)}")
        return value


class DraftFilePreview(BaseModel):
    """The head of a file's extracted text, so the user can confirm the parse
    worked — a PDF that extracted as gibberish is otherwise invisible."""

    id: uuid.UUID
    filename: str
    char_count: int
    excerpt: str
