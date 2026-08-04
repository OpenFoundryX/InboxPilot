"""The registry is the only place the command surface is written down.

The drift guard is the point of this file: `handlers.execute` grew a
twelfth action type once already, and nothing would have noticed if a
thirteenth arrived with no way to reach it under a slash-only rule.
"""

import inspect
import re

from services.commands import handlers, registry


def test_names_are_unique_and_lowercase():
    names = [c.name for c in registry.COMMANDS]
    assert len(names) == len(set(names))
    assert all(n == n.lower() and n.isalpha() for n in names)


def test_help_is_not_a_command_name():
    # `/help` is resolved before lookup; a command named "help" would shadow it.
    assert registry.lookup(registry.HELP_NAME) is None


def test_lookup_is_case_insensitive():
    assert registry.lookup("VIP") is registry.lookup("vip")
    assert registry.lookup("nope") is None


def test_usage_starts_with_its_own_name():
    for c in registry.COMMANDS:
        assert c.usage.startswith(f"/{c.name}"), c.name


def test_fixed_actions_declare_exactly_their_own_type():
    for c in registry.COMMANDS:
        if c.fixed_action is None:
            continue
        assert c.action_types == (c.fixed_action["type"],), c.name


def test_every_registered_type_is_executable():
    registered = {t for c in registry.COMMANDS for t in c.action_types}
    assert registered <= handlers.ACTION_TYPES


def test_every_executable_type_is_reachable_from_a_named_command():
    # `/do` allows everything, so it would trivially satisfy this. Exclude it:
    # the point is that each type has a *discoverable* home in the menu.
    named = {t for c in registry.COMMANDS if c.name != "do" for t in c.action_types}
    assert named == handlers.ACTION_TYPES


def test_do_allows_every_type():
    do = registry.lookup("do")
    assert do is not None
    assert set(do.action_types) == handlers.ACTION_TYPES


def test_action_types_constant_matches_what_execute_dispatches_on():
    """Keeps `ACTION_TYPES` honest against the if-chain it sits beside."""
    source = inspect.getsource(handlers.execute)
    dispatched = set(re.findall(r'atype\s*(?:==|in)\s*\(?\s*"([^"]+)"', source))
    dispatched |= set(re.findall(r'atype\s+in\s+\("[^"]+",\s*"([^"]+)"', source))
    assert dispatched == handlers.ACTION_TYPES


def test_help_text_lists_every_command_as_a_code_span():
    text = registry.help_text()
    for c in registry.COMMANDS:
        assert f"`/{c.name}`" in text
        assert c.summary in text
