import uuid

from models.chat import ROLE_ASSISTANT, ROLE_USER, STATUS_PENDING
from services.chat import store


def test_derive_title_trims_and_truncates():
    assert store.derive_title("  did pradeep send the sheet?  ") == "did pradeep send the sheet?"
    long = "x" * 200
    title = store.derive_title(long)
    assert len(title) <= 60
    assert title.endswith("…")


def test_derive_title_falls_back_for_blank():
    assert store.derive_title("   ") == "New chat"


def test_derive_title_uses_the_first_line_only():
    assert store.derive_title("first line\nsecond line") == "first line"


async def test_create_and_list_conversations(db, user):
    a = await store.create_conversation(db, user.id, "first question")
    b = await store.create_conversation(db, user.id, "second question")
    await store.add_message(db, a, ROLE_USER, "first question")
    await store.add_message(db, b, ROLE_USER, "second question")

    rows = await store.list_conversations(db, user.id)

    # Most recently active first.
    assert [r.id for r in rows] == [b.id, a.id]
    assert rows[0].title == "second question"


async def test_conversations_are_scoped_to_their_owner(db, user, other_user):
    mine = await store.create_conversation(db, user.id, "mine")

    assert await store.get_conversation(db, other_user.id, mine.id) is None
    assert await store.get_conversation(db, user.id, mine.id) is not None
    assert await store.list_conversations(db, other_user.id) == []


async def test_add_message_advances_last_message_at(db, user):
    conv = await store.create_conversation(db, user.id, "q")
    assert conv.last_message_at is None

    first = await store.add_message(db, conv, ROLE_USER, "q")
    assert conv.last_message_at is not None
    before = conv.last_message_at

    second = await store.add_message(db, conv, ROLE_ASSISTANT, "a")
    assert conv.last_message_at >= before
    assert [m.id for m in await store.list_messages(db, conv.id)] == [first.id, second.id]


async def test_add_message_stores_pending_actions(db, user):
    conv = await store.create_conversation(db, user.id, "make a label")
    msg = await store.add_message(
        db,
        conv,
        ROLE_ASSISTANT,
        "",
        actions=[{"type": "create_label", "name": "Receipts"}],
        action_status=STATUS_PENDING,
    )

    assert msg.action_status == STATUS_PENDING
    assert msg.actions[0]["name"] == "Receipts"
    assert msg.action_results == []


async def test_recent_turns_returns_the_last_n_in_order(db, user):
    conv = await store.create_conversation(db, user.id, "q0")
    for i in range(5):
        await store.add_message(db, conv, ROLE_USER, f"q{i}")
        await store.add_message(db, conv, ROLE_ASSISTANT, f"a{i}")

    turns = await store.recent_turns(db, conv.id, n=4)

    assert [t["content"] for t in turns] == ["q3", "a3", "q4", "a4"]
    assert turns[0]["role"] == ROLE_USER


async def test_recent_turns_skips_empty_content(db, user):
    conv = await store.create_conversation(db, user.id, "q")
    await store.add_message(db, conv, ROLE_USER, "real")
    await store.add_message(db, conv, ROLE_ASSISTANT, "")

    assert [t["content"] for t in await store.recent_turns(db, conv.id)] == ["real"]


async def test_delete_conversation_is_owner_scoped(db, user, other_user):
    conv = await store.create_conversation(db, user.id, "q")
    await store.add_message(db, conv, ROLE_USER, "q")

    assert await store.delete_conversation(db, other_user.id, conv.id) is False
    assert await store.delete_conversation(db, user.id, conv.id) is True
    assert await store.get_conversation(db, user.id, conv.id) is None
    # Cascade removed the messages too.
    assert await store.list_messages(db, conv.id) == []


async def test_delete_missing_conversation_returns_false(db, user):
    assert await store.delete_conversation(db, user.id, uuid.uuid4()) is False


async def test_get_message_for_user_is_owner_scoped(db, user, other_user):
    conv = await store.create_conversation(db, user.id, "q")
    msg = await store.add_message(db, conv, ROLE_ASSISTANT, "a")

    assert await store.get_message_for_user(db, other_user.id, msg.id) is None
    found = await store.get_message_for_user(db, user.id, msg.id)
    assert found is not None and found.id == msg.id
