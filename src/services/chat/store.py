"""Async DB helpers for web chat conversations and messages."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.chat import (
    STATUS_NONE,
    ChatConversation,
    ChatMessage,
)

TITLE_MAX = 60
DEFAULT_TITLE = "New chat"


def derive_title(message: str) -> str:
    """A sidebar title from the first user message — no extra LLM call."""
    first_line = (message or "").strip().splitlines()[0].strip() if (message or "").strip() else ""
    if not first_line:
        return DEFAULT_TITLE
    if len(first_line) <= TITLE_MAX:
        return first_line
    return first_line[: TITLE_MAX - 1].rstrip() + "…"


async def list_conversations(db: AsyncSession, user_id: uuid.UUID) -> list[ChatConversation]:
    """Most recently active first; never-used conversations sort last."""
    result = await db.scalars(
        select(ChatConversation)
        .where(ChatConversation.user_id == user_id)
        .order_by(
            ChatConversation.last_message_at.desc().nullslast(),
            ChatConversation.created_at.desc(),
        )
    )
    return list(result)


async def get_conversation(
    db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> ChatConversation | None:
    return await db.scalar(
        select(ChatConversation).where(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user_id,
        )
    )


async def create_conversation(
    db: AsyncSession, user_id: uuid.UUID, first_message: str
) -> ChatConversation:
    row = ChatConversation(user_id=user_id, title=derive_title(first_message))
    db.add(row)
    await db.flush()
    return row


async def delete_conversation(
    db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """True when a conversation belonging to this user was removed."""
    row = await get_conversation(db, user_id, conversation_id)
    if row is None:
        return False
    await db.execute(delete(ChatMessage).where(ChatMessage.conversation_id == row.id))
    await db.delete(row)
    await db.flush()
    return True


async def list_messages(db: AsyncSession, conversation_id: uuid.UUID) -> list[ChatMessage]:
    result = await db.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    )
    return list(result)


async def add_message(
    db: AsyncSession,
    conversation: ChatConversation,
    role: str,
    content: str,
    *,
    sources: list | None = None,
    actions: list | None = None,
    action_status: str = STATUS_NONE,
) -> ChatMessage:
    # created_at is stamped client-side (not left to the column's server_default):
    # Postgres's now() is fixed for the whole transaction, so several messages
    # added within one request/session would otherwise tie on created_at and
    # sort by their (random) id instead of insertion order.
    now = datetime.now(timezone.utc)
    row = ChatMessage(
        conversation_id=conversation.id,
        role=role,
        content=content,
        sources=sources or [],
        actions=actions or [],
        action_status=action_status,
        action_results=[],
        created_at=now,
    )
    db.add(row)
    conversation.last_message_at = now
    await db.flush()
    return row


async def recent_turns(db: AsyncSession, conversation_id: uuid.UUID, n: int = 6) -> list[dict]:
    """The last `n` non-empty messages as {"role", "content"}, oldest first."""
    messages = await list_messages(db, conversation_id)
    return [{"role": m.role, "content": m.content} for m in messages if m.content][-n:]


async def get_message_for_user(
    db: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID
) -> ChatMessage | None:
    """A message, but only if it belongs to a conversation this user owns."""
    return await db.scalar(
        select(ChatMessage)
        .join(ChatConversation, ChatMessage.conversation_id == ChatConversation.id)
        .where(ChatMessage.id == message_id, ChatConversation.user_id == user_id)
    )
