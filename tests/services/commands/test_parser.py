"""Prompt composition and the out-of-scope filter.

No OpenAI call happens here: `build_system` is pure, and the filtering test
replaces the client with a stub that returns a fixed JSON body.
"""

import json

import pytest

from services.commands import parser
from services.commands.handlers import ACTION_TYPES


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
    """Stands in for `OpenAI`, capturing the system prompt it was handed."""

    def __init__(self, payload):
        # A str payload is returned verbatim, so a test can feed it junk.
        self.content = payload if isinstance(payload, str) else json.dumps(payload)
        self.system_prompt = None
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.system_prompt = kwargs["messages"][0]["content"]
        return _FakeResponse(self.content)


@pytest.fixture
def fake_openai(monkeypatch):
    """Install a fake client and hand the test a way to set its reply."""

    def install(payload):
        client = _FakeClient(payload)
        monkeypatch.setattr(parser, "_client", lambda: client)
        monkeypatch.setattr(parser.settings, "OPENAI_API_KEY", "test-key")
        return client

    return install


def test_specs_cover_every_executable_type():
    assert set(parser._ACTION_SPECS) == ACTION_TYPES


def test_build_system_with_none_includes_every_type():
    prompt = parser.build_system(None)
    for atype in ACTION_TYPES:
        assert f'"type": "{atype}"' in prompt


def test_build_system_subset_excludes_the_others():
    prompt = parser.build_system(("create_rule",))
    assert '"type": "create_rule"' in prompt
    assert '"type": "set_routine"' not in prompt
    assert '"type": "add_vip"' not in prompt


def test_build_system_always_includes_the_global_rules():
    for types in (None, ("create_rule",), ("set_reminder",)):
        assert "NEVER output placeholder values" in parser.build_system(types)


def test_build_system_ordering_is_stable():
    a = parser.build_system(("create_rule", "add_vip"))
    b = parser.build_system(("add_vip", "create_rule"))
    assert a == b


def test_parse_command_default_composes_every_type(fake_openai):
    """The email-to-self path calls with three arguments and must not change."""
    client = fake_openai({"actions": [], "summary": ""})
    parser.parse_command("Subject", "body", "UTC")
    assert client.system_prompt == parser.build_system(None)


def test_parse_command_passes_only_the_allowed_types(fake_openai):
    client = fake_openai({"actions": [], "summary": ""})
    parser.parse_command(None, "archive stuff", "UTC", allowed_types=("create_rule",))
    assert client.system_prompt == parser.build_system(("create_rule",))


def test_out_of_scope_actions_are_dropped(fake_openai):
    fake_openai(
        {
            "actions": [
                {"type": "create_rule", "criteria": {"from": "x@y.com"}, "archive": True},
                {"type": "set_routine", "times_per_day": 3},
            ],
            "summary": "archive x",
        }
    )
    out = parser.parse_command(None, "archive x", "UTC", allowed_types=("create_rule",))
    assert [a["type"] for a in out["actions"]] == ["create_rule"]
    assert out["summary"] == "archive x"


def test_no_filtering_when_allowed_types_is_none(fake_openai):
    fake_openai({"actions": [{"type": "set_routine", "times_per_day": 3}], "summary": "batch"})
    out = parser.parse_command("s", "b", "UTC")
    assert [a["type"] for a in out["actions"]] == ["set_routine"]


def test_malformed_json_returns_no_actions(fake_openai):
    fake_openai("not json at all")
    out = parser.parse_command(None, "anything", "UTC")
    assert out == {"actions": [], "summary": ""}


def test_reply_prefix_still_short_circuits(fake_openai):
    fake_openai({"actions": [{"type": "create_label", "name": "X"}], "summary": "x"})
    out = parser.parse_command(f"{parser.REPLY_SUBJECT_PREFIX} done", "body", "UTC")
    assert out == {"actions": [], "summary": ""}
