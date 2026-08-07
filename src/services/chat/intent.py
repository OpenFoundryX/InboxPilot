"""Decide what a chat message is asking for — and, if it wants a change, which
slash command would make it.

This module used to sit on the path to a state change: `INTENT_COMMAND` went
straight to `parse_command`, which raised a confirm card. That put a
probabilistic call in front of an irreversible one, and when it guessed wrong
the user got a card with nothing to approve and no answer either.

Chat now mutates nothing without an explicit slash command, so this classifier
has a smaller and much more forgiving job: it decides whether to *suggest*. A
wrong `command` costs the user one sentence beneath a real answer.

The command surface is injected from `services.commands.registry`, so the model
is picking from the same list `/help` prints and the web menu offers — it
cannot invent a command that does not exist, and `_suggestion` drops it to
`/do` if it tries anyway.
"""

import json
from dataclasses import dataclass

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from services.commands.registry import COMMANDS, lookup

log = get_logger(__name__)

# About the assistant or the conversation; answerable without the mailbox.
INTENT_SMALLTALK = "smalltalk"
# Wants to read/find/summarise mail. Nothing in the account changes.
INTENT_QUESTION = "question"
# Wants something changed. We do not change it — we suggest the slash command.
INTENT_COMMAND = "command"

_INTENTS = {INTENT_SMALLTALK, INTENT_QUESTION, INTENT_COMMAND}

# The escape hatch, used when the model names a command that doesn't exist.
_FALLBACK_COMMAND = "do"

# How many prior turns the classifier sees. Enough to resolve a follow-up
# ("do it at 9am instead") without paying for the whole transcript.
_HISTORY_TURNS = 4

_COMMAND_LIST = "\n".join(f"- {c.name}: {c.summary}" for c in COMMANDS)

SYSTEM = f"""You route one message in a chat between a user and InboxOS, their email
assistant. Return ONLY JSON: {{"intent": "...", "command": "..." | null}}.

"intent" is one of:
- "{INTENT_SMALLTALK}" — about the assistant or the conversation rather than the
  mailbox: greetings, "who are you", "what can you do", "how does batching work",
  thanks, chit-chat. Anything answerable without opening their mailbox.
- "{INTENT_QUESTION}" — the user wants to READ, FIND, SEE, COUNT or SUMMARISE mail.
  Examples: "show me my important emails", "what did I miss?", "any invoices from
  AWS?", "did Pradeep reply?", "catch me up", "summarise this week".
- "{INTENT_COMMAND}" — the user wants something CHANGED or DONE: create/delete a
  label, add or remove a VIP, create a rule, change the delivery routine or quiet
  hours, turn a scheduled routine on or off, set a reminder, archive/star/trash/label
  mail, or send them an email right now.

"command" is required when intent is "{INTENT_COMMAND}", and null otherwise. It names
the slash command that would carry out what they asked, chosen from EXACTLY this list:
{_COMMAND_LIST}

Use "do" when the request is a real change but no other name fits.

Rules:
- Showing something IN THIS CHAT is "{INTENT_QUESTION}", never "{INTENT_COMMAND}".
- show/find/list/see/what/which/who/did/any/how many/summarise/catch me up -> question.
- make/create/delete/add/remove/stop/start/turn on/turn off/always/from now on/set/
  schedule/remind me/archive/trash/mute/send me -> command.
- A question ABOUT a capability ("can you batch my mail?") is "{INTENT_SMALLTALK}"; an
  instruction to use it ("batch my mail") is "{INTENT_COMMAND}".
- When torn between question and command, answer "{INTENT_QUESTION}"."""


@dataclass(frozen=True)
class Intent:
    """What the message wants, and which command would deliver it.

    `command` is a registry command name, set only when `kind` is
    `INTENT_COMMAND`. It is a suggestion the user must act on — nothing
    downstream executes it.
    """

    kind: str
    command: str | None = None


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _suggestion(raw: object) -> str:
    """Coerce the model's command name onto the real surface."""
    if isinstance(raw, str):
        found = lookup(raw)
        if found is not None:
            return found.name
    log.info("chat.intent_command_unmapped", got=raw)
    return _FALLBACK_COMMAND


def classify(message: str, history: list[dict] | None = None) -> Intent:
    """Return the `Intent` for this message.

    Falls back to `INTENT_QUESTION` on anything unexpected — a malformed
    response should cost the user an answer they didn't need, not a suggestion
    they can't use.
    """
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    turns = [
        f"{h.get('role', 'user')}: {(h.get('content') or '')[:300]}"
        for h in (history or [])[-_HISTORY_TURNS:]
        if h.get("content")
    ]
    context = "Earlier in this chat:\n" + "\n".join(turns) + "\n\n" if turns else ""

    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{context}Classify this message:\n{message[:2000]}"},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("chat.intent_bad_json", raw=raw)
        return Intent(INTENT_QUESTION)

    kind = data.get("intent")
    if kind not in _INTENTS:
        log.warning("chat.intent_unknown", intent=kind)
        return Intent(INTENT_QUESTION)

    if kind != INTENT_COMMAND:
        return Intent(kind)
    return Intent(kind, _suggestion(data.get("command")))
