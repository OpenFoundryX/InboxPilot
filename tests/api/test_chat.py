"""API-level tests: ownership isolation and the confirm state machine.

The SSE path is exercised end-to-end manually (Task 15); here we cover the
authorization and state rules, which are the parts that must never regress.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

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
