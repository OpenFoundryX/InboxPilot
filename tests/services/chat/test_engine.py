"""The engine is deliberately DB-free, so these tests need no Postgres and no
network — only fakes for the parser, the retriever, and the answer stream."""

from services.chat import engine
from services.chat.sources.base import Excerpt


def _excerpt(subject="Invoice 42") -> Excerpt:
    return Excerpt(
        kind="email",
        title=subject,
        sender="billing@aws.com",
        date="2026-07-20T10:00:00+00:00",
        link="https://mail.google.com/mail/u/me@example.com/#all/t9",
        text="Your invoice is attached",
        ref_id="m9",
        thread_id="t9",
        attachment_count=1,
    )


class FakeRetriever:
    kind = "email"

    def __init__(self, excerpts=None, error: Exception | None = None):
        self.excerpts = excerpts or []
        self.error = error
        self.calls = []

    async def retrieve(self, user_id, question, history):
        self.calls.append((user_id, question, history))
        if self.error:
            raise self.error
        return self.excerpts


def _no_actions(subject, body, tz=None):
    return {"actions": [], "summary": ""}


def _fake_stream(*texts):
    async def stream(message, history, excerpts):
        for t in texts:
            yield t

    return stream


async def _collect(**over):
    kwargs = dict(
        user_id="u1",
        message="any invoices from AWS?",
        history=[],
        timezone="Asia/Kolkata",
        retriever=FakeRetriever([_excerpt()]),
        gmail_connected=True,
    )
    kwargs.update(over)
    return [ev async for ev in engine.turn_events(**kwargs)]


async def test_answer_path_event_order(monkeypatch):
    monkeypatch.setattr(engine, "parse_command", _no_actions)
    monkeypatch.setattr(engine, "stream_answer", _fake_stream("Yes, ", "two."))

    events = await _collect()
    names = [name for name, _ in events]

    assert names == [
        "stage",
        "stage",
        "sources",
        "stage",
        "stage",
        "token",
        "token",
    ]
    assert events[0][1]["label"] == "Reading your question"
    assert events[1][1]["label"] == "Searching your mail"
    assert events[2][1]["sources"][0]["title"] == "Invoice 42"
    assert events[3][1]["label"] == "Found 1 email"
    assert events[4][1]["label"] == "Writing answer"
    assert "".join(e[1]["text"] for e in events if e[0] == "token") == "Yes, two."


async def test_found_label_pluralises(monkeypatch):
    monkeypatch.setattr(engine, "parse_command", _no_actions)
    monkeypatch.setattr(engine, "stream_answer", _fake_stream("ok"))

    events = await _collect(retriever=FakeRetriever([_excerpt("a"), _excerpt("b")]))
    labels = [p["label"] for n, p in events if n == "stage"]
    assert "Found 2 emails" in labels


async def test_no_results_still_answers(monkeypatch):
    monkeypatch.setattr(engine, "parse_command", _no_actions)
    monkeypatch.setattr(engine, "stream_answer", _fake_stream("I couldn't find it."))

    events = await _collect(retriever=FakeRetriever([]))
    assert ("sources", {"sources": []}) in events
    assert any(n == "token" for n, _ in events)


async def test_command_path_proposes_and_executes_nothing(monkeypatch):
    actions = [{"type": "create_label", "name": "Receipts"}]
    monkeypatch.setattr(
        engine,
        "parse_command",
        lambda s, b, tz=None: {"actions": actions, "summary": "make a label"},
    )
    monkeypatch.setattr(engine, "stream_answer", _fake_stream("should not be called"))
    retriever = FakeRetriever([_excerpt()])

    events = await _collect(message="make a Receipts label", retriever=retriever)

    assert [n for n, _ in events] == ["stage", "actions"]
    payload = events[1][1]
    assert payload["raw"] == actions
    assert payload["actions"][0]["label"] == "Create Gmail label “Receipts”"
    assert payload["summary"] == "make a label"
    # No retrieval and no answer streaming on the command path.
    assert retriever.calls == []


async def test_not_connected_is_graceful(monkeypatch):
    monkeypatch.setattr(engine, "parse_command", _no_actions)
    retriever = FakeRetriever([_excerpt()])

    events = await _collect(gmail_connected=False, retriever=retriever)

    assert [n for n, _ in events] == ["stage", "sources", "token"]
    assert events[2][1]["text"] == engine.NOT_CONNECTED_MESSAGE
    assert "/onboarding/connect" in engine.NOT_CONNECTED_MESSAGE
    assert retriever.calls == []


async def test_retrieval_failure_still_answers(monkeypatch):
    monkeypatch.setattr(engine, "parse_command", _no_actions)
    monkeypatch.setattr(engine, "stream_answer", _fake_stream("Nothing found."))

    events = await _collect(retriever=FakeRetriever(error=RuntimeError("composio down")))

    # A dead retriever degrades to an ungrounded answer rather than a 500.
    assert ("sources", {"sources": []}) in events
    assert any(n == "token" for n, _ in events)


async def test_history_reaches_the_retriever(monkeypatch):
    monkeypatch.setattr(engine, "parse_command", _no_actions)
    monkeypatch.setattr(engine, "stream_answer", _fake_stream("ok"))
    retriever = FakeRetriever([])
    history = [{"role": "user", "content": "earlier question"}]

    await _collect(retriever=retriever, history=history)

    assert retriever.calls[0][2] == history
