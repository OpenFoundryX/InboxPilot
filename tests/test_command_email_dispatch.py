"""Which sent mail counts as a command, and which is just mail you sent.

The command surface is "email yourself an instruction". The trigger that
replaced the old `from:me to:me` sweep kept only the `from:me` half — the SENT
label — so every outgoing message reached the command handler, which answers
anything it cannot parse as an action. These tests pin the missing half.
"""

import base64
import uuid

import pytest

from integrations.google.mime import canonical_address, recipient_addresses
from models.users import User
from workers.jobs import gmail_poll

ME = "nilesh@chronon.co.in"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(*, to: str, labels: list[str], cc: str | None = None) -> dict:
    header_list = [{"name": "To", "value": to}, {"name": "Subject", "value": "hello"}]
    if cc:
        header_list.append({"name": "Cc", "value": cc})
    return {
        "id": "m1",
        "threadId": "t1",
        "labelIds": labels,
        "snippet": "hello",
        "payload": {
            "mimeType": "text/plain",
            "headers": header_list,
            "body": {"data": _b64("some body")},
        },
    }


@pytest.fixture
def queued(monkeypatch):
    """Run `_dispatch` with Gmail, Redis and Celery replaced by capture lists."""
    commands: list[dict] = []
    classifications: list[dict] = []

    monkeypatch.setattr(gmail_poll, "_hold_label_id", lambda user_id: None)
    monkeypatch.setattr(gmail_poll, "is_ours_sync", lambda message_id: False)
    monkeypatch.setattr(gmail_poll, "claim_event_sync", lambda user_id, message_id: True)
    monkeypatch.setattr(
        gmail_poll.handle_command_email,
        "delay",
        lambda *args, **kwargs: commands.append(kwargs),
    )
    monkeypatch.setattr(
        gmail_poll.classify_new_email,
        "delay",
        lambda *args, **kwargs: classifications.append(kwargs),
    )

    def run(message: dict) -> tuple[list[dict], list[dict]]:
        monkeypatch.setattr(gmail_poll.gmail, "get_message", lambda *a, **k: message)
        gmail_poll._dispatch(
            "user-1",
            [{"id": message["id"], "labelIds": message["labelIds"]}],
            account_email=ME,
        )
        return commands, classifications

    return run


def test_sent_to_someone_else_is_not_a_command(queued):
    """The bug: mail you sent to a third party used to be run as a command."""
    commands, classifications = queued(_message(to="oleksii@llmapi.ai", labels=["SENT"]))
    assert commands == []
    # Nor is outgoing mail classified — the old sweep excluded `from:me` too.
    assert classifications == []


def test_sent_to_yourself_is_a_command(queued):
    commands, _ = queued(_message(to=f"Nilesh Pant <{ME}>", labels=["SENT"]))
    assert len(commands) == 1


def test_sent_to_your_inboxos_alias_is_a_command(queued):
    commands, _ = queued(_message(to="nilesh+inboxos@chronon.co.in", labels=["SENT"]))
    assert len(commands) == 1


def test_yourself_on_cc_is_a_command(queued):
    commands, _ = queued(_message(to="oleksii@llmapi.ai", labels=["SENT"], cc=ME))
    assert len(commands) == 1


def test_incoming_mail_still_classifies(queued):
    commands, classifications = queued(_message(to=ME, labels=["INBOX"]))
    assert commands == []
    assert len(classifications) == 1


def test_dispatch_passes_recipients_to_the_command_task(queued):
    """The handler re-checks self-addressing, so it needs the recipients."""
    commands, _ = queued(_message(to=ME, labels=["SENT"]))
    assert commands[0]["recipients"] == [ME]


class TestAddressHelpers:
    def test_canonical_strips_display_name_tag_and_case(self):
        assert canonical_address("Nilesh Pant <Nilesh+inboxos@Chronon.co.in>") == ME

    def test_recipients_reads_to_cc_and_bcc(self):
        header_map = {"to": "a@x.com, B@X.com", "cc": "c@x.com", "bcc": "d+tag@x.com"}
        assert recipient_addresses(header_map) == ["a@x.com", "b@x.com", "c@x.com", "d@x.com"]

    def test_unparseable_recipients_are_dropped(self):
        assert recipient_addresses({"to": "undisclosed-recipients:;"}) == []


class _FakeDb:
    """Enough session for `_handle`: one `get` and a no-op commit."""

    def __init__(self, user):
        self._user = user
        self.committed = False

    async def get(self, model, uid):
        return self._user

    async def commit(self):
        self.committed = True


@pytest.fixture
def handler(monkeypatch):
    """`_handle` with Gmail, Redis, the LLM and the connection cache stubbed."""
    from workers.jobs import handle_command_email as mod

    sent: list[tuple] = []
    monkeypatch.setattr(mod, "get_connection", lambda user_id: None)
    monkeypatch.setattr(mod.gmail_ops, "resolve_label_id", lambda user_id, name: "Label_9")
    monkeypatch.setattr(mod.gmail_ops, "add_label", lambda *a, **k: None)
    monkeypatch.setattr(mod, "allow_reply", lambda thread_id: True)
    monkeypatch.setattr(mod, "remember_ours", lambda message_id: None)
    monkeypatch.setattr(mod, "parse_command", lambda *a, **k: {"actions": []})
    monkeypatch.setattr(mod, "answer_question", lambda *a, **k: "here is your answer")
    monkeypatch.setattr(
        mod.gmail,
        "reply_in_thread",
        lambda user_id, thread_id, to, body, is_html=False: sent.append((to, body)) or "sent-1",
    )

    async def _settings(db, uid):
        return type("S", (), {"timezone": "Asia/Kolkata"})()

    monkeypatch.setattr(mod, "get_or_create_settings", _settings)
    return mod, sent


async def _run(mod, *, recipients):
    user = User(id=uuid.uuid4(), email=ME)
    db = _FakeDb(user)
    return await mod._handle(
        db,
        str(user.id),
        message_id="m1",
        subject="hello",
        body="some body",
        thread_id="t1",
        label_ids=["SENT"],
        recipients=recipients,
    )


async def test_handler_refuses_a_message_sent_to_someone_else(handler):
    """Defence in depth: even if the poller enqueues it, no reply goes out."""
    mod, sent = handler
    result = await _run(mod, recipients=["oleksii@llmapi.ai"])
    assert result == {"skipped": "not_self_addressed"}
    assert sent == []


async def test_handler_refuses_an_enqueue_with_no_recipients(handler):
    """A task queued before this field existed is not assumed to be self-mail."""
    mod, sent = handler
    result = await _run(mod, recipients=None)
    assert result == {"skipped": "not_self_addressed"}
    assert sent == []


async def test_handler_answers_a_note_you_sent_yourself(handler):
    """The feature itself still works: self-addressed mail gets its reply."""
    mod, sent = handler
    result = await _run(mod, recipients=[ME, "oleksii@llmapi.ai"])
    assert result == {"actions": 0, "replied": True}
    assert len(sent) == 1
    to, body = sent[0]
    # Addressed to the user, never to the other people on the original.
    assert to == ME
    assert "here is your answer" in body


def test_backfill_fetch_excludes_your_own_mail(monkeypatch):
    """The same rule, one layer over: onboarding classifies mail you received.

    `fetch_recent_emails` feeds `backfill.queue_unlabelled`, which enqueues a
    classify task per message. Without `-from:me` the onboarding sweep put
    category labels on the user's own sent mail.
    """
    from integrations.google import gmail

    seen: list[str] = []
    monkeypatch.setattr(
        gmail, "fetch_by_query", lambda user_id, query, max_results=None: seen.append(query) or []
    )

    gmail.fetch_recent_emails("user-1", days=30)

    assert seen == ["newer_than:30d -from:me"]
