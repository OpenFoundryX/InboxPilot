"""API-level tests: ownership isolation and the confirm state machine.

Full end-to-end SSE verification against real Gmail/OpenAI is manual (Task 13);
here we cover the authorization and state rules, which are the parts that must
never regress, plus an SSE-level smoke test that exercises the frame sequence
and the setup-failure path with `engine.turn_events` faked out.
"""

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from main import create_app
from models.chat import STATUS_CONFIRMED, STATUS_NONE, STATUS_PENDING, STATUS_REJECTED
from services.auth.dependencies import get_current_user
from services.chat import store


@pytest.fixture
def app(db, user):
    application = create_app()
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: user
    return application


@pytest.fixture
def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _ReusedSession:
    """Stands in for `SessionLocal()` in the SSE tests below.

    `_ask_stream` deliberately opens its own `SessionLocal()` instead of using
    the request-scoped `DbSession` (see `src/api/v1/chat.py` module
    docstring). A *real* `SessionLocal()` opens a second, independent Postgres
    transaction — which can't see the `user` row the `db`/`user` fixtures
    created but never committed, and would hang or FK-fail. So for these
    frame-sequence smoke tests we hand `_ask_stream` the same fixture session
    instead, wrapped in `SessionLocal`'s async-context-manager shape. That
    keeps everything in one transaction, which is enough to verify the SSE
    event sequence and persistence; it does not exercise the real
    cross-session lifecycle, which is why that is verified manually
    (Task 13).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events = []
    for block in raw.strip("\n").split("\n\n"):
        if not block:
            continue
        event_line, data_line = block.split("\n", 1)
        events.append(
            (event_line.removeprefix("event: "), json.loads(data_line.removeprefix("data: ")))
        )
    return events


async def test_list_conversations_returns_only_the_callers(client, db, user, other_user):
    mine = await store.create_conversation(db, user.id, "mine")
    await store.add_message(db, mine, "user", "mine")
    theirs = await store.create_conversation(db, other_user.id, "theirs")
    await store.add_message(db, theirs, "user", "theirs")

    res = await client.get("/v1/chat/conversations")

    assert res.status_code == 200
    body = res.json()
    assert [c["title"] for c in body] == ["mine"]


async def test_get_other_users_conversation_is_404(client, db, other_user):
    theirs = await store.create_conversation(db, other_user.id, "theirs")

    res = await client.get(f"/v1/chat/conversations/{theirs.id}")

    assert res.status_code == 404


async def test_get_conversation_returns_messages_in_order(client, db, user):
    conv = await store.create_conversation(db, user.id, "q")
    await store.add_message(db, conv, "user", "q")
    await store.add_message(db, conv, "assistant", "a")

    res = await client.get(f"/v1/chat/conversations/{conv.id}")

    assert res.status_code == 200
    body = res.json()
    assert body["title"] == "q"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["action_status"] == STATUS_NONE


async def test_delete_conversation(client, db, user, other_user):
    conv = await store.create_conversation(db, user.id, "q")
    theirs = await store.create_conversation(db, other_user.id, "theirs")

    assert (await client.delete(f"/v1/chat/conversations/{theirs.id}")).status_code == 404
    assert (await client.delete(f"/v1/chat/conversations/{conv.id}")).status_code == 204
    assert (await client.get(f"/v1/chat/conversations/{conv.id}")).status_code == 404


async def test_confirm_executes_pending_actions(client, db, user, monkeypatch):
    from api.v1 import chat as chat_api

    async def fake_execute(session, uid, action):
        return f"Created label '{action['name']}'"

    monkeypatch.setattr(chat_api, "execute_action", fake_execute)

    conv = await store.create_conversation(db, user.id, "make a label")
    msg = await store.add_message(
        db, conv, "assistant", "",
        actions=[{"type": "create_label", "name": "Receipts"}],
        action_status=STATUS_PENDING,
    )

    res = await client.post(f"/v1/chat/messages/{msg.id}/confirm", json={"approve": True})

    assert res.status_code == 200
    body = res.json()
    assert body["action_status"] == STATUS_CONFIRMED
    assert body["action_results"] == ["Created label 'Receipts'"]


async def test_confirm_twice_is_a_conflict(client, db, user, monkeypatch):
    from api.v1 import chat as chat_api

    async def fake_execute(session, uid, action):
        return "done"

    monkeypatch.setattr(chat_api, "execute_action", fake_execute)

    conv = await store.create_conversation(db, user.id, "q")
    msg = await store.add_message(
        db, conv, "assistant", "", actions=[{"type": "catch_up_now"}],
        action_status=STATUS_PENDING,
    )

    first = await client.post(f"/v1/chat/messages/{msg.id}/confirm", json={"approve": True})
    second = await client.post(f"/v1/chat/messages/{msg.id}/confirm", json={"approve": True})

    assert first.status_code == 200
    assert second.status_code == 409


async def test_reject_executes_nothing(client, db, user, monkeypatch):
    from api.v1 import chat as chat_api

    calls = []

    async def fake_execute(session, uid, action):
        calls.append(action)
        return "ran"

    monkeypatch.setattr(chat_api, "execute_action", fake_execute)

    conv = await store.create_conversation(db, user.id, "trash everything")
    msg = await store.add_message(
        db, conv, "assistant", "",
        actions=[{"type": "create_rule", "criteria": {"from": "x@y.com"}, "trash": True}],
        action_status=STATUS_PENDING,
    )

    res = await client.post(f"/v1/chat/messages/{msg.id}/confirm", json={"approve": False})

    assert res.status_code == 200
    assert res.json()["action_status"] == STATUS_REJECTED
    assert calls == []


async def test_one_failing_action_does_not_stop_the_rest(client, db, user, monkeypatch):
    from api.v1 import chat as chat_api

    async def fake_execute(session, uid, action):
        if action["type"] == "create_label":
            raise RuntimeError("Gmail said no")
        return "second ok"

    monkeypatch.setattr(chat_api, "execute_action", fake_execute)

    conv = await store.create_conversation(db, user.id, "two things")
    msg = await store.add_message(
        db, conv, "assistant", "",
        actions=[{"type": "create_label", "name": "X"}, {"type": "catch_up_now"}],
        action_status=STATUS_PENDING,
    )

    res = await client.post(f"/v1/chat/messages/{msg.id}/confirm", json={"approve": True})

    results = res.json()["action_results"]
    assert results[0].startswith("failed:")
    assert "Gmail said no" in results[0]
    assert results[1] == "second ok"


async def test_confirm_other_users_message_is_404(client, db, other_user):
    conv = await store.create_conversation(db, other_user.id, "theirs")
    msg = await store.add_message(
        db, conv, "assistant", "", actions=[{"type": "catch_up_now"}],
        action_status=STATUS_PENDING,
    )

    res = await client.post(f"/v1/chat/messages/{msg.id}/confirm", json={"approve": True})

    assert res.status_code == 404


async def test_confirm_missing_message_is_404(client):
    res = await client.post(f"/v1/chat/messages/{uuid.uuid4()}/confirm", json={"approve": True})
    assert res.status_code == 404


async def test_ask_streams_conversation_then_done_with_a_confirmable_message_id(
    client, db, user, monkeypatch
):
    """Regression coverage for the SSE frame contract.

    The client can only confirm proposed actions using the id in the `done`
    frame, so that id must be the id of the message actually persisted.
    """
    from api.v1 import chat as chat_api

    monkeypatch.setattr(chat_api, "SessionLocal", lambda: _ReusedSession(db))
    monkeypatch.setattr(chat_api.gmail, "is_connected", lambda uid: True)

    async def fake_turn_events(**kwargs):
        yield chat_api.engine.EV_STAGE, {"label": "Thinking"}
        yield chat_api.engine.EV_TOKEN, {"text": "Hello"}
        yield chat_api.engine.EV_TOKEN, {"text": " world"}

    monkeypatch.setattr(chat_api.engine, "turn_events", fake_turn_events)

    async with client.stream("POST", "/v1/chat/ask", json={"message": "hi"}) as res:
        assert res.status_code == 200
        raw = "".join([chunk async for chunk in res.aiter_text()])

    events = _parse_sse(raw)
    names = [name for name, _ in events]
    assert names[0] == "conversation"
    assert names[-1] == "done"
    assert "error" not in names
    assert "stage" in names
    assert names.count("token") == 2

    message_id = uuid.UUID(events[-1][1]["message_id"])
    persisted = await store.get_message_for_user(db, user.id, message_id)
    assert persisted is not None
    assert persisted.role == "assistant"
    assert persisted.content == "Hello world"
    assert persisted.action_status == STATUS_NONE


async def test_ask_setup_failure_still_emits_error_and_done_and_persists_a_message(
    client, db, user, monkeypatch
):
    """Regression test for the finding: a setup call (here, `gmail.is_connected`,
    a real network call to Composio in production) raising before the engine
    even starts must not kill the stream silently. The client must still get
    an `error` frame and a `done` frame, and the transcript must not have a
    hole — the user's question needs a (possibly empty) assistant reply after
    it, not nothing.
    """
    from api.v1 import chat as chat_api

    monkeypatch.setattr(chat_api, "SessionLocal", lambda: _ReusedSession(db))

    def _boom(uid):
        raise RuntimeError("Composio is down")

    monkeypatch.setattr(chat_api.gmail, "is_connected", _boom)

    async with client.stream("POST", "/v1/chat/ask", json={"message": "hi"}) as res:
        assert res.status_code == 200
        raw = "".join([chunk async for chunk in res.aiter_text()])

    events = _parse_sse(raw)
    names = [name for name, _ in events]
    assert names[0] == "conversation"
    assert "error" in names
    assert names[-1] == "done"

    message_id = uuid.UUID(events[-1][1]["message_id"])
    persisted = await store.get_message_for_user(db, user.id, message_id)
    assert persisted is not None
    assert persisted.role == "assistant"
    assert persisted.action_status == STATUS_NONE
