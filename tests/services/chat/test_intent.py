"""The classifier's new job: it suggests, it no longer decides.

Its output can no longer cause a state change, so these tests are about the
suggestion staying inside the command surface — a suggested `/frobnicate`
would render as a chip that does nothing.
"""

import json

import pytest

from services.chat import intent
from services.commands import registry


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeClient:
    def __init__(self, content):
        self.content = content
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        return _FakeResponse(self.content)


@pytest.fixture
def reply(monkeypatch):
    def install(payload):
        content = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr(intent, "_client", lambda: _FakeClient(content))
        monkeypatch.setattr(intent.settings, "OPENAI_API_KEY", "test-key")

    return install


def test_question_carries_no_command(reply):
    reply({"intent": "question"})
    result = intent.classify("what did I miss?")
    assert result.kind == intent.INTENT_QUESTION
    assert result.command is None


def test_smalltalk_carries_no_command(reply):
    reply({"intent": "smalltalk", "command": "rule"})
    result = intent.classify("who are you?")
    assert result.kind == intent.INTENT_SMALLTALK
    assert result.command is None


def test_command_carries_the_suggested_name(reply):
    reply({"intent": "command", "command": "rule"})
    result = intent.classify("archive all the marketing emails")
    assert result.kind == intent.INTENT_COMMAND
    assert result.command == "rule"


def test_unknown_suggestion_falls_back_to_do(reply):
    reply({"intent": "command", "command": "frobnicate"})
    assert intent.classify("do something odd").command == "do"


def test_missing_suggestion_falls_back_to_do(reply):
    reply({"intent": "command"})
    assert intent.classify("do something odd").command == "do"


def test_malformed_json_degrades_to_question(reply):
    reply("this is not json")
    result = intent.classify("anything")
    assert result.kind == intent.INTENT_QUESTION
    assert result.command is None


def test_unknown_intent_degrades_to_question(reply):
    reply({"intent": "banana"})
    assert intent.classify("anything").kind == intent.INTENT_QUESTION


def test_prompt_lists_the_real_command_surface():
    for c in registry.COMMANDS:
        assert c.name in intent.SYSTEM
        assert c.summary in intent.SYSTEM


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(intent.settings, "OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError):
        intent.classify("anything")
