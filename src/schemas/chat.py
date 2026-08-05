"""Pydantic schemas for the web chat API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.chat.describe import describe_actions


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

    @field_validator("actions", mode="after")
    @classmethod
    def _readable(cls, actions: list[dict]) -> list[dict]:
        """Serve actions as the confirm card needs them, not as stored.

        The column keeps the raw parser output because that is what
        `/messages/{id}/confirm` executes. The client renders `label` and
        `detail`, which only `describe_actions` produces — so a transcript
        reload used to hand the card `{"type": "catch_up_now"}` and it drew a
        row of empty labels above an Approve button.
        """
        return describe_actions(actions)


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


class CommandRead(BaseModel):
    """One slash command, as the web autocomplete menu needs it.

    Served rather than duplicated in TypeScript: an eleven-row list with
    descriptions kept in two repos drifts, and the menu is the only place
    users discover these.
    """

    name: str
    summary: str
    usage: str
