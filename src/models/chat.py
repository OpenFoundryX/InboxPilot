"""Web chat models.

A conversation is a titled thread of messages between one user and InboxOS in
the web app (the email surface has no conversations — it threads in Gmail).

Proposed-but-unconfirmed actions live on the assistant message that proposed
them: `actions` holds the parser output, `action_status` tracks the decision,
and `action_results` holds the handler result lines once executed. Keeping them
on the message means a confirm card survives a page reload with no separate
pending-action table to reconcile.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# Lifecycle of the actions proposed by one assistant message.
STATUS_NONE = "none"  # plain answer, nothing to confirm
STATUS_PENDING = "pending"  # awaiting the user's decision
STATUS_CONFIRMED = "confirmed"  # approved and executed
STATUS_REJECTED = "rejected"  # dismissed, never executed


class ChatConversation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "chat_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # Denormalised so the sidebar can order without touching chat_messages.
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ChatMessage(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "chat_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)

    sources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    actions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    action_status: Mapped[str] = mapped_column(String(16), default=STATUS_NONE, nullable=False)
    action_results: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
