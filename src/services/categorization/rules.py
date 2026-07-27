"""Deterministic rule matching. Pure functions — no DB, no network, no LLM.

Kept separate from the pipeline because this is where the fiddly semantics live
(address parsing, domain normalisation, case folding) and it is the one part of
categorization that can be reasoned about in isolation.
"""

from dataclasses import dataclass
from email.utils import parseaddr

from models.categorization import (
    MATCH_BODY_KEYWORD,
    MATCH_SENDER_ADDRESS,
    MATCH_SENDER_DOMAIN,
    MATCH_SUBJECT_KEYWORD,
)


@dataclass(frozen=True)
class RuleSnapshot:
    """A rule detached from the DB session."""

    match_type: str
    match_value: str
    action: str
    category_key: str | None


def _address(sender: str | None) -> str:
    """The bare address out of a From header: 'Bo <bo@acme.com>' -> 'bo@acme.com'."""
    return parseaddr(sender or "")[1].strip().casefold()


def _domain(sender: str | None) -> str:
    address = _address(sender)
    return address.rpartition("@")[2] if "@" in address else ""


def matches(
    rule: RuleSnapshot, sender: str | None, subject: str | None, snippet: str | None
) -> bool:
    value = rule.match_value.strip().casefold()
    if not value:
        return False

    if rule.match_type == MATCH_SENDER_ADDRESS:
        return _address(sender) == value
    if rule.match_type == MATCH_SENDER_DOMAIN:
        # Accept the value with or without a leading '@'.
        return _domain(sender) == value.lstrip("@")
    if rule.match_type == MATCH_SUBJECT_KEYWORD:
        return value in (subject or "").casefold()
    if rule.match_type == MATCH_BODY_KEYWORD:
        # The snippet, not the full body — the trigger payload has no body.
        return value in (snippet or "").casefold()
    return False


def first_match(
    rules: list[RuleSnapshot],
    sender: str | None,
    subject: str | None,
    snippet: str | None,
) -> RuleSnapshot | None:
    """First matching rule wins. `rules` must already be in priority order."""
    for rule in rules:
        if matches(rule, sender, subject, snippet):
            return rule
    return None
