"""Web chat API.

`POST /ask` streams Server-Sent Events. Two things about it are deliberate:

1. The generator opens its own `SessionLocal()`. The request-scoped `DbSession`
   from `get_db` closes as soon as the response starts — before the generator
   has finished writing the assistant message.
2. Actions are never executed here. The engine proposes them, they are stored
   `pending` on the assistant message, and `/messages/{id}/confirm` runs them.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from api.deps import DbSession
from core.database import SessionLocal
from core.exceptions import ConflictError, NotFoundError
from core.logging import get_logger
from integrations.composio import gmail
from models.chat import (
    ROLE_ASSISTANT,
    ROLE_USER,
    STATUS_CONFIRMED,
    STATUS_NONE,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from models.users import User
from schemas.chat import (
    AskRequest,
    ConfirmRequest,
    ConversationDetail,
    ConversationRead,
    MessageRead,
)
from services.auth.dependencies import get_current_user
from services.billing.dependencies import EntitledUser
from services.chat import engine, store
from services.chat.sources.email_source import EmailRetriever
from services.commands.handlers import execute as execute_action
from services.mailman.store import get_or_create_settings

log = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

CurrentUser = Annotated[User, Depends(get_current_user)]

SSE_HEADERS = {
    # `no-transform` is the part that matters, and it is not decoration: the web
    # app reaches this endpoint through Next's rewrite proxy, whose compression
    # middleware gzips anything `text/*`. Nothing in that pipe calls flush(), so
    # every frame sat in the gzip buffer until the stream closed and the whole
    # answer landed in the browser at once — streaming that only worked under
    # curl, which doesn't ask for gzip. `compression` (and nginx) skip a
    # response marked no-transform, which restores frame-by-frame delivery.
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Tells nginx-style proxies not to buffer, which would defeat streaming.
    "X-Accel-Buffering": "no",
}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(user: CurrentUser, db: DbSession):
    return await store.list_conversations(db, user.id)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession):
    conv = await store.get_conversation(db, user.id, conversation_id)
    if conv is None:
        raise NotFoundError("Conversation not found")
    messages = await store.list_messages(db, conv.id)
    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        last_message_at=conv.last_message_at,
        messages=[MessageRead.model_validate(m) for m in messages],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, user: CurrentUser, db: DbSession):
    if not await store.delete_conversation(db, user.id, conversation_id):
        raise NotFoundError("Conversation not found")
    return Response(status_code=204)


@router.post("/ask")
async def ask(payload: AskRequest, user: EntitledUser) -> StreamingResponse:
    """Answer one turn, streamed as SSE.

    Gated on `EntitledUser`: every turn is an uncapped LLM call, and a locked
    account calling this in a loop had no gate and no cost. Read endpoints
    (`/conversations*`) and the actions a past turn already proposed
    (`/messages/{id}/confirm`) stay open — the point is to stop *new* answers
    from being generated, not to hide history or block actions that are
    already individually gated in `services.commands.handlers.execute`.
    """
    return StreamingResponse(
        _ask_stream(user_id=user.id, account_email=user.email, payload=payload),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _ask_stream(
    *, user_id: uuid.UUID, account_email: str, payload: AskRequest
) -> AsyncIterator[str]:
    async with SessionLocal() as db:
        try:
            conv = await _resolve_conversation(db, user_id, payload)
        except NotFoundError as exc:
            yield _sse("error", {"message": exc.detail})
            return

        yield _sse("conversation", {"id": str(conv.id), "title": conv.title})

        history = await store.recent_turns(db, conv.id)
        await store.add_message(db, conv, ROLE_USER, payload.message)
        await db.commit()

        content: list[str] = []
        sources: list[dict] = []
        raw_actions: list[dict] = []
        # Safe defaults so the except-path below never references an unbound
        # name if either setup call raises. An unknown connection state
        # degrades to "not connected" rather than risking a live retrieval.
        timezone = "UTC"
        gmail_connected = False

        try:
            settings_row = await get_or_create_settings(db, user_id)
            timezone = settings_row.timezone
            gmail_connected = await run_in_threadpool(gmail.is_connected, str(user_id))

            events = engine.turn_events(
                user_id=str(user_id),
                message=payload.message,
                history=history,
                timezone=timezone,
                retriever=EmailRetriever(account_email=account_email),
                gmail_connected=gmail_connected,
            )
            async for name, data in events:
                if name == engine.EV_TOKEN:
                    content.append(data["text"])
                elif name == engine.EV_SOURCES:
                    sources = data["sources"]
                elif name == engine.EV_ACTIONS:
                    raw_actions = data["raw"]
                    # The client needs the message id to confirm against, and
                    # that only exists once the row is written — so send the
                    # described actions now and the id in `done`.
                    data = {"actions": data["actions"], "summary": data["summary"]}
                yield _sse(name, data)
        except Exception as exc:
            log.exception("chat.turn_failed", user_id=str(user_id))
            yield _sse("error", {"message": _friendly_error(exc)})

        # Persist whatever we produced, even on a partial failure, so the
        # transcript has no hole.
        status = STATUS_PENDING if raw_actions else STATUS_NONE
        message = await store.add_message(
            db,
            conv,
            ROLE_ASSISTANT,
            "".join(content),
            sources=sources,
            actions=raw_actions,
            action_status=status,
        )
        await db.commit()
        yield _sse("done", {"message_id": str(message.id)})


async def _resolve_conversation(db, user_id: uuid.UUID, payload: AskRequest):
    if payload.conversation_id is None:
        return await store.create_conversation(db, user_id, payload.message)
    conv = await store.get_conversation(db, user_id, payload.conversation_id)
    if conv is None:
        raise NotFoundError("Conversation not found")
    return conv


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "OPENAI_API_KEY" in text:
        return "Chat isn't configured yet — the server is missing an OpenAI API key."
    return "Something went wrong while answering. Please try again."


@router.post("/messages/{message_id}/confirm", response_model=MessageRead)
async def confirm(
    message_id: uuid.UUID, payload: ConfirmRequest, user: CurrentUser, db: DbSession
):
    """Execute (or dismiss) the actions proposed by an assistant message."""
    message = await store.get_message_for_user(db, user.id, message_id)
    if message is None:
        raise NotFoundError("Message not found")
    if message.action_status != STATUS_PENDING:
        raise ConflictError(f"These actions are already {message.action_status}")

    if not payload.approve:
        message.action_status = STATUS_REJECTED
        message.action_results = []
        await db.flush()
        return message

    results: list[str] = []
    for action in message.actions:
        try:
            results.append(await execute_action(db, user.id, action))
        except Exception as exc:
            # One bad action must not block the others, matching the email path.
            log.warning("chat.action_failed", user_id=str(user.id), action=action, exc_info=True)
            results.append(f"failed: {exc}")

    message.action_status = STATUS_CONFIRMED
    message.action_results = results
    await db.flush()
    return message
