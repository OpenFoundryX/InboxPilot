"""One test per routing branch.

`turn_events` is deliberately free of database access, so every branch here is
reachable with fakes and no I/O — which is the whole reason the routing logic
lives in this module rather than in the API layer.
"""

import pytest

from services.chat import engine
from services.chat.intent import INTENT_COMMAND, INTENT_QUESTION, INTENT_SMALLTALK, Intent


class FakeRetriever:
    def __init__(self, excerpts=None):
        self.excerpts = excerpts or []
        self.calls = 0

    async def retrieve(self, user_id, message, history):
        self.calls += 1
        return self.excerpts


def exploding_parser(*args, **kwargs):
    raise AssertionError("parse_command must not be called on this path")


async def collect(**overrides):
    """Drive one turn to completion and return the events as a list."""
    kwargs = {
        "user_id": "u1",
        "message": "hello",
        "history": [],
        "timezone": "UTC",
        "retriever": FakeRetriever(),
        "gmail_connected": True,
    }
    kwargs.update(overrides)
    return [ev async for ev in engine.turn_events(**kwargs)]


def texts(events):
    return "".join(d["text"] for name, d in events if name == engine.EV_TOKEN)


def actions(events):
    for name, data in events:
        if name == engine.EV_ACTIONS:
            return data
    return None


@pytest.fixture
def no_model(monkeypatch):
    """Fail loudly if any model call is made. Individual tests opt back in."""
    monkeypatch.setattr(engine, "parse_command", exploding_parser)

    def classify(*args, **kwargs):
        raise AssertionError("classify must not be called on this path")

    monkeypatch.setattr(engine, "classify", classify)


@pytest.fixture
def answering(monkeypatch):
    """Replace the two streaming answer paths with fixed text."""

    async def answer(message, history, excerpts):
        yield "ANSWER"

    async def smalltalk(message, history):
        yield "SMALLTALK"

    monkeypatch.setattr(engine, "stream_answer", answer)
    monkeypatch.setattr(engine, "stream_smalltalk", smalltalk)


# --- slash branch ------------------------------------------------------


async def test_help_renders_the_registry_without_a_model_call(no_model):
    events = await collect(message="/help")
    assert "`/rule`" in texts(events)
    assert actions(events) is None


async def test_bare_slash_renders_help(no_model):
    assert "`/catchup`" in texts(await collect(message="/"))


async def test_unknown_command_names_it_and_shows_help(no_model):
    body = texts(await collect(message="/sdfsd whatever"))
    assert "/sdfsd" in body
    assert "`/rule`" in body


async def test_fixed_action_command_needs_no_model_call(no_model):
    events = await collect(message="/catchup")
    assert actions(events)["raw"] == [{"type": "catch_up_now"}]


async def test_fixed_action_ignores_trailing_text(no_model):
    events = await collect(message="/briefing about last week")
    assert actions(events)["raw"] == [{"type": "send_briefing_now"}]


async def test_command_needing_args_with_none_gives_usage_and_no_model_call(no_model):
    body = texts(await collect(message="/rule"))
    assert "/rule archive everything from newsletters@x.com" in body
    assert actions(await collect(message="/rule")) is None


async def test_command_with_args_proposes_the_parsed_actions(monkeypatch):
    captured = {}

    def parse(subject, body, tz, allowed_types=None):
        captured["body"] = body
        captured["allowed_types"] = allowed_types
        return {
            "actions": [{"type": "create_rule", "archive": True}],
            "summary": "archive newsletters",
        }

    monkeypatch.setattr(engine, "parse_command", parse)
    events = await collect(message="/rule archive everything from news@x.com")

    assert captured["body"] == "archive everything from news@x.com"
    assert captured["allowed_types"] == ("create_rule",)
    assert actions(events)["raw"] == [{"type": "create_rule", "archive": True}]
    assert "Archive newsletters" in texts(events)


async def test_failed_parse_does_not_fall_through_to_answering(monkeypatch, answering):
    monkeypatch.setattr(engine, "parse_command", lambda *a, **k: {"actions": [], "summary": ""})
    retriever = FakeRetriever()
    events = await collect(message="/rule something incoherent", retriever=retriever)

    assert "ANSWER" not in texts(events)
    assert retriever.calls == 0
    assert "/rule archive everything from newsletters@x.com" in texts(events)


async def test_slash_never_reaches_the_classifier(no_model):
    # `no_model` already explodes on classify; this asserts the whole set.
    for message in ("/help", "/", "/nope", "/catchup", "/rule"):
        await collect(message=message)


# --- prose branch ------------------------------------------------------


async def test_smalltalk_answers_from_the_persona(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_SMALLTALK))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    events = await collect(message="who are you?")
    assert texts(events) == "SMALLTALK"


async def test_question_answers_and_proposes_nothing(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_QUESTION))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    events = await collect(message="what did I miss?")
    assert texts(events) == "ANSWER"
    assert actions(events) is None


async def test_prose_command_answers_then_nudges(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "rule"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    message = "can you archive all the marketing emails"
    body = texts(await collect(message=message))

    assert body.startswith("ANSWER")
    assert f"`/rule {message}`" in body


async def test_prose_command_raises_no_confirm_card(monkeypatch, answering):
    """The whole point: a misfiring classifier can no longer produce a card."""
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "rule"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    events = await collect(message="show me my important emails")
    assert actions(events) is None


async def test_classifier_failure_answers_with_no_nudge(monkeypatch, answering):
    def boom(*args, **kwargs):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(engine, "classify", boom)
    body = texts(await collect(message="what did I miss?"))
    assert body == "ANSWER"


async def test_not_connected_path_emits_no_nudge(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "rule"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    body = texts(await collect(message="archive everything", gmail_connected=False))
    assert body == engine.NOT_CONNECTED_MESSAGE


async def test_slash_still_works_when_gmail_is_not_connected(no_model):
    """Commands are proposals; connection is checked when they execute."""
    events = await collect(message="/catchup", gmail_connected=False)
    assert actions(events)["raw"] == [{"type": "catch_up_now"}]


async def test_nudge_truncates_a_very_long_message(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "do"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    body = texts(await collect(message="x" * 500))
    assert "x" * 201 not in body
