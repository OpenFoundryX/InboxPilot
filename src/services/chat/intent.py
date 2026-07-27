"""Decide what a chat message is actually asking for.

The email surface can afford to treat every message as a candidate command: a
mail you send yourself is usually an instruction. A chat is not. Most messages
there are questions ("show me my important emails") and some are about the
assistant itself ("who are you?"). Routing those straight into `parse_command`
produced the worst possible answer — a read-only question came back as a
confirm card with nothing worth confirming, and no reply at all.

So chat classifies first and parses second: only `INTENT_COMMAND` ever reaches
`parse_command`, and therefore only a genuine state change ever reaches a
confirm card. Every ambiguity resolves towards answering, because a needless
answer costs a sentence while a needless confirm card stops the conversation
dead.
"""

import json

from openai import OpenAI

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)

# About the assistant or the conversation; answerable without the mailbox.
INTENT_SMALLTALK = "smalltalk"
# Wants to read/find/summarise mail. Nothing in the account changes.
INTENT_QUESTION = "question"
# Wants the assistant to change a setting or act on their behalf.
INTENT_COMMAND = "command"

_INTENTS = {INTENT_SMALLTALK, INTENT_QUESTION, INTENT_COMMAND}

# How many prior turns the classifier sees. Enough to resolve a follow-up
# ("do it at 9am instead") without paying for the whole transcript.
_HISTORY_TURNS = 4

_SYSTEM = f"""You route one message in a chat between a user and InboxOS, their email
assistant. Return ONLY JSON: {{"intent": "{INTENT_SMALLTALK}" | "{INTENT_QUESTION}" | "{INTENT_COMMAND}"}}.

- "{INTENT_SMALLTALK}" — the message is about the assistant or the conversation rather
  than the mailbox: greetings, "who are you", "what can you do", "how does batching
  work", "are you an AI", thanks, chit-chat. Anything answerable without opening
  their mailbox.
- "{INTENT_QUESTION}" — the user wants to READ, FIND, SEE, COUNT or SUMMARISE mail.
  Nothing in their account changes. Examples: "show me my important emails", "what did
  I miss?", "any invoices from AWS?", "did Pradeep reply?", "what's in my inbox", "catch
  me up", "summarise this week".
- "{INTENT_COMMAND}" — the user wants something CHANGED or DONE: create/delete a label,
  add or remove a VIP, create a rule, change the delivery routine or quiet hours, turn a
  scheduled routine on or off, set a reminder, archive/star/trash/label mail, or send
  them an email right now.

Rules:
- Showing something IN THIS CHAT is "{INTENT_QUESTION}", never "{INTENT_COMMAND}".
  "Show me my important emails" is a question — the user wants to see them here, not to
  have anything sent, filed or changed.
- show/find/list/see/what/which/who/did/any/how many/summarise/catch me up -> question.
- make/create/delete/add/remove/stop/start/turn on/turn off/always/from now on/set/
  schedule/remind me/archive/trash/mute/send me -> command.
- A question ABOUT a capability ("can you batch my mail?") is "{INTENT_SMALLTALK}"; an
  instruction to use it ("batch my mail") is "{INTENT_COMMAND}".
- When torn between question and command, answer "{INTENT_QUESTION}"."""


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def classify(message: str, history: list[dict] | None = None) -> str:
    """Return one of the INTENT_* constants for this message.

    Falls back to `INTENT_QUESTION` on anything unexpected — a malformed
    response should cost the user an answer they didn't need, not a confirm
    card they can't act on.
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
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"{context}Classify this message:\n{message[:2000]}"},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        intent = json.loads(raw).get("intent")
    except json.JSONDecodeError:
        log.warning("chat.intent_bad_json", raw=raw)
        return INTENT_QUESTION

    if intent not in _INTENTS:
        log.warning("chat.intent_unknown", intent=intent)
        return INTENT_QUESTION
    return intent
