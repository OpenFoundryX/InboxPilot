"""Pydantic schemas for the web chat API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    # Free-form on purpose: `sources` shapes follow the retriever (email today,
    # meeting notes later) and `actions` follow the command parser.
    sources: list[dict] = []
    actions: list[dict] = []
    action_status: str
    action_results: list[str] = []
    created_at: datetime


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    last_message_at: datetime | None = None


class ConversationDetail(ConversationRead):
    messages: list[MessageRead] = []


class AskRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=4000)


class ConfirmRequest(BaseModel):
    approve: bool
