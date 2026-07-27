"""One chat turn, as a stream of events.

Deliberately free of database access: the API layer owns persistence and this
module owns the decision-making. That keeps the interesting logic — intent
routing, event ordering, graceful degradation — unit-testable with nothing but
fakes.

Three paths, chosen by `intent.classify`:

- smalltalk ("who are you?", "what can you do?") — answered from the persona,
  with no mailbox access and no Gmail connection required.
- question ("show me my important emails") — retrieve context, stream a
  grounded answer.
- command ("archive everything from x@y.com") — *propose* the actions and
  stop; execution needs an explicit confirm.

Only the command path can raise a confirm card. That distinction is the whole
point of classifying: a question that came back as a card was a dead end for
the user, since there was nothing to approve and no answer either.
"""

from collections.abc import AsyncIterator

from fastapi.concurrency import run_in_threadpool
from openai import AsyncOpenAI

from core.config import settings
from core.logging import get_logger
from services.chat.describe import describe_actions
from services.chat.intent import (
    INTENT_COMMAND,
    INTENT_QUESTION,
    INTENT_SMALLTALK,
    classify,
)
from services.chat.sources.base import Excerpt, Retriever
from services.commands.ask import ANSWER_RULES
from services.commands.parser import parse_command
from services.persona import CAPABILITIES

log = get_logger(__name__)

EV_STAGE = "stage"
EV_TOKEN = "token"
EV_SOURCES = "sources"
EV_ACTIONS = "actions"

# How many prior turns are replayed to the answering model.
HISTORY_TURNS = 6

# Defence-in-depth cap on excerpt body length when rendering the corpus. The
# `Retriever` protocol (Task 4) makes no promise that sources truncate their
# text — today's `EmailRetriever` happens to cap at 800 chars already, but a
# future meeting-notes or Notion retriever could return a full document. This
# bound keeps a runaway source from ballooning the answer prompt.
_EXCERPT_TEXT_CAP = 800

NOT_CONNECTED_MESSAGE = (
    "I can't read your mail yet — your Gmail account isn't connected. "
    "Connect it on the [setup page](/onboarding/connect) and ask me again."
)

_CHAT_ANSWER_SYS = ANSWER_RULES + (
    "\n\nThis is a live chat, not an email: do NOT sign off, and do not repeat the "
    "user's question back to them. The sources are also listed separately beneath "
    "your answer, so keep inline links to the ones you actually reference."
)

_CHAT_SMALLTALK_SYS = f"""You are InboxOS, the user's email assistant, replying in a live
web chat. Be warm and brief — two or three sentences, light Markdown, no sign-off, no
subject line, and no bullet-point dump unless they asked for the full list.

{CAPABILITIES}

You have NOT looked at their mailbox for this reply, so never describe or invent
anything that is in it — offer to go look instead. If they ask for something you
cannot do, say so plainly and name the closest thing you can."""


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def _stream_completion(system: str, turns: list[dict], user: str) -> AsyncIterator[str]:
    stream = await _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0.3,
        messages=[{"role": "system", "content": system}, *turns, {"role": "user", "content": user}],
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


def _replayed(history: list[dict]) -> list[dict]:
    return [
        {"role": h["role"], "content": h["content"]}
        for h in history[-HISTORY_TURNS:]
        if h.get("content")
    ]


def _excerpts_as_corpus(excerpts: list[Excerpt]) -> str:
    """Render source-agnostic `Excerpt`s into the prompt corpus.

    Deliberately mirrors `services.commands.ask.build_corpus`'s layout —
    same field order, same "N file(s)"/"none" attachment phrasing, same
    "---"-joined blocks — so the model sees one consistent corpus shape
    across the email and chat surfaces. It is a separate function, not a
    call to `build_corpus`, because that helper is typed to `EmailSummary`
    while this seam must stay source-agnostic per the `Retriever` protocol.
    """
    blocks = []
    for ex in excerpts:
        atts = f"{ex.attachment_count} file(s)" if ex.attachment_count else "none"
        text = (ex.text or "")[:_EXCERPT_TEXT_CAP]
        blocks.append(
            f"From: {ex.sender}\nDate: {ex.date}\nSubject: {ex.title}\n"
            f"Attachments: {atts}\nLink: {ex.link}\n{text}"
        )
    return "\n\n---\n\n".join(blocks)


async def stream_answer(
    message: str, history: list[dict], excerpts: list[Excerpt]
) -> AsyncIterator[str]:
    """Yield answer deltas from the model."""
    if excerpts:
        context = f"\n\nRelevant emails from their inbox:\n{_excerpts_as_corpus(excerpts)}"
    else:
        context = "\n\n(No relevant emails were found in their inbox.)"

    stream = _stream_completion(
        _CHAT_ANSWER_SYS, _replayed(history), f"Question: {message}{context}"
    )
    async for delta in stream:
        yield delta


async def stream_smalltalk(message: str, history: list[dict]) -> AsyncIterator[str]:
    """Yield deltas for a message about the assistant itself, mailbox untouched."""
    async for delta in _stream_completion(_CHAT_SMALLTALK_SYS, _replayed(history), message):
        yield delta


async def _intent(user_id: str, message: str, history: list[dict]) -> str:
    """Classify the message, degrading to the answer path if the router fails.

    A classifier outage must not turn every message into a confirm card, and it
    must not fail the turn either: `stream_answer` will re-raise a genuine
    configuration problem (a missing API key) with a friendlier message.
    """
    try:
        intent = await run_in_threadpool(classify, message, history)
    except Exception:
        log.warning("chat.classify_failed", user_id=user_id, exc_info=True)
        return INTENT_QUESTION
    log.info("chat.intent", user_id=user_id, intent=intent)
    return intent


def _proposal_lead_in(summary: str) -> str:
    """One line of context above the confirm card."""
    if not summary:
        return "Here's what I'll do — approve below and I'll go ahead."
    summary = summary[0].upper() + summary[1:]
    return f"{summary.rstrip('.')} — approve below and I'll go ahead."


async def turn_events(
    *,
    user_id: str,
    message: str,
    history: list[dict],
    timezone: str,
    retriever: Retriever,
    gmail_connected: bool,
) -> AsyncIterator[tuple[str, dict]]:
    """Drive one turn, yielding (event_name, payload) pairs."""
    yield EV_STAGE, {"label": "Reading your question"}

    intent = await _intent(user_id, message, history)

    if intent == INTENT_SMALLTALK:
        async for delta in stream_smalltalk(message, history):
            yield EV_TOKEN, {"text": delta}
        return

    if intent == INTENT_COMMAND:
        # `parse_command` reads a subject line too; chat has none.
        parsed = await run_in_threadpool(parse_command, None, message, timezone)
        actions = parsed.get("actions") or []
        summary = parsed.get("summary") or ""
        if actions:
            log.info("chat.actions_proposed", user_id=user_id, count=len(actions))
            # A card on its own reads as a demand with no explanation, so say
            # what is about to happen before asking them to approve it.
            yield EV_TOKEN, {"text": _proposal_lead_in(summary)}
            yield EV_ACTIONS, {
                "actions": describe_actions(actions),
                "raw": actions,
                "summary": summary,
            }
            return
        # Classified as a command, but nothing actionable came back. Answering
        # beats a silent turn, so fall through to the question path.
        log.info("chat.command_without_actions", user_id=user_id)

    if not gmail_connected:
        yield EV_SOURCES, {"sources": []}
        yield EV_TOKEN, {"text": NOT_CONNECTED_MESSAGE}
        return

    yield EV_STAGE, {"label": "Searching your mail"}
    try:
        excerpts = await retriever.retrieve(user_id, message, history)
    except Exception:
        # A dead source should degrade the answer, not fail the turn.
        log.warning("chat.retrieve_failed", user_id=user_id, exc_info=True)
        excerpts = []

    yield EV_SOURCES, {"sources": [e.as_dict() for e in excerpts]}
    noun = "email" if len(excerpts) == 1 else "emails"
    yield EV_STAGE, {"label": f"Found {len(excerpts)} {noun}"}

    yield EV_STAGE, {"label": "Writing answer"}
    async for delta in stream_answer(message, history, excerpts):
        yield EV_TOKEN, {"text": delta}
