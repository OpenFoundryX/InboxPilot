"""One chat turn, as a stream of events.

Deliberately free of database access: the API layer owns persistence and this
module owns the decision-making. That keeps the interesting logic — intent
routing, event ordering, graceful degradation — unit-testable with nothing but
fakes.

Two gates, in order:

- A message that opens with "/" is a command. It is resolved against the
  registry and proposed as actions — no classification, and for the four
  fixed-action commands, no model call at all.
- Anything else is classified. Smalltalk answers from the persona, a question
  retrieves and answers, and a message that *reads* like a command is answered
  too, with the slash form suggested beneath it.

Prose can no longer mutate anything. That is the point: the classifier used to
sit in front of `parse_command`, so a wrong guess produced a confirm card with
nothing worth approving and no answer either. Now a wrong guess costs one
sentence under a real answer.
"""

from collections.abc import AsyncIterator

from fastapi.concurrency import run_in_threadpool
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionStreamOptionsParam

from core.config import settings
from core.logging import get_logger
from services.chat.describe import describe_actions
from services.chat.intent import (
    INTENT_COMMAND,
    INTENT_QUESTION,
    INTENT_SMALLTALK,
    Intent,
    classify,
)
from services.chat.sources.base import Excerpt, Retriever
from services.commands import slash
from services.commands.ask import ANSWER_RULES
from services.commands.parser import parse_command
from services.commands.registry import help_text
from services.persona import CAPABILITIES

log = get_logger(__name__)

EV_STAGE = "stage"
EV_TOKEN = "token"
EV_SOURCES = "sources"
EV_ACTIONS = "actions"
# What the answer actually cost, straight from the provider. Emitted after the
# last text token, so the client has the full reply before the figure arrives.
EV_USAGE = "usage"

# How many prior turns are replayed to the answering model.
HISTORY_TURNS = 6

# Defence-in-depth cap on excerpt body length when rendering the corpus. The
# `Retriever` protocol makes no promise that sources truncate their text, so a
# future meeting-notes or Notion retriever returning a full document must not be
# able to balloon the answer prompt.
#
# It is a *backstop*, not the working limit, and the difference matters: at 800
# it was silently re-truncating `EmailRetriever`'s excerpts to a fifth of what
# they carry, including the thread context appended to them. A cap set at the
# same value a source happens to use stops being defence-in-depth and starts
# being the behaviour — so this now sits comfortably above it.
_EXCERPT_TEXT_CAP = 6000

NOT_CONNECTED_MESSAGE = (
    "I can't read your mail yet — your Gmail account isn't connected. "
    "Connect it on the [setup page](/onboarding/connect) and ask me again."
)

# How much of the user's message is echoed back inside the suggested command.
# Long enough to carry a real instruction, short enough that the chip stays
# readable on one or two lines.
_NUDGE_MESSAGE_CAP = 200

NUDGE_TEMPLATE = (
    "\n\nI only make changes from a slash command — I can't act on a plain "
    "message. Run `{prefill}` to do it."
)


def _nudge(command: str, message: str) -> str:
    """The suggestion appended under an answer to a prose command.

    The prefill echoes the user's own words because that is exactly the input
    the constrained parser wants: `/rule <what they said>`.
    """
    prefill = f"/{command} {' '.join(message.split())[:_NUDGE_MESSAGE_CAP]}".rstrip()
    return NUDGE_TEMPLATE.format(prefill=prefill)


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


async def _stream_completion(
    system: str, turns: list[dict], user: str
) -> AsyncIterator[tuple[str, dict]]:
    """Stream one completion as `(event, payload)` pairs.

    Yields events rather than bare text so token usage can ride the same
    channel. `include_usage` makes the provider send one final chunk carrying
    exact counts — no local tokenizer to keep in agreement, and no guessing.
    That chunk has empty `choices`, which is how it is told apart from content.
    """
    model = settings.SUMMARY_MODEL
    # Typed rather than a bare dict so the SDK's overloads resolve.
    usage_option: ChatCompletionStreamOptionsParam = {"include_usage": True}
    stream = await _client().chat.completions.create(
        # Same reasoning as the email "Ask anything" flow: synthesising a
        # grounded answer across a mailbox is where the cheap model's hedging,
        # generic prose shows, and it is the only step here worth the upgrade.
        # Retrieval and intent classification stay on OPENAI_MODEL.
        model=model,
        temperature=0.3,
        messages=[{"role": "system", "content": system}, *turns, {"role": "user", "content": user}],
        stream=True,
        stream_options=usage_option,
    )
    async for chunk in stream:
        if usage := getattr(chunk, "usage", None):
            yield (
                EV_USAGE,
                {
                    "model": model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
            )
            continue
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield EV_TOKEN, {"text": delta}


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
) -> AsyncIterator[tuple[str, dict]]:
    """Yield answer events (text deltas, then usage) from the model."""
    if excerpts:
        context = f"\n\nRelevant emails from their inbox:\n{_excerpts_as_corpus(excerpts)}"
    else:
        context = "\n\n(No relevant emails were found in their inbox.)"

    stream = _stream_completion(
        _CHAT_ANSWER_SYS, _replayed(history), f"Question: {message}{context}"
    )
    async for event in stream:
        yield event


async def stream_smalltalk(message: str, history: list[dict]) -> AsyncIterator[tuple[str, dict]]:
    """Yield events for a message about the assistant itself, mailbox untouched."""
    async for event in _stream_completion(_CHAT_SMALLTALK_SYS, _replayed(history), message):
        yield event


async def _intent(user_id: str, message: str, history: list[dict]) -> Intent:
    """Classify the message, degrading to the answer path if the router fails.

    A classifier outage must not fail the turn: `stream_answer` will re-raise a
    genuine configuration problem (a missing API key) with a friendlier message.
    """
    try:
        result = await run_in_threadpool(classify, message, history)
    except Exception:
        log.warning("chat.classify_failed", user_id=user_id, exc_info=True)
        return Intent(INTENT_QUESTION)
    log.info("chat.intent", user_id=user_id, intent=result.kind, command=result.command)
    return result


def _proposal_lead_in(summary: str) -> str:
    """One line of context above the confirm card."""
    if not summary:
        return "Here's what I'll do — approve below and I'll go ahead."
    summary = summary[0].upper() + summary[1:]
    return f"{summary.rstrip('.')} — approve below and I'll go ahead."


def _propose(proposed: list[dict], summary: str) -> list[tuple[str, dict]]:
    """The lead-in and the confirm card, in that order.

    A card on its own reads as a demand with no explanation, so say what is
    about to happen before asking anyone to approve it.
    """
    return [
        (EV_TOKEN, {"text": _proposal_lead_in(summary)}),
        (
            EV_ACTIONS,
            {"actions": describe_actions(proposed), "raw": proposed, "summary": summary},
        ),
    ]


async def _command_events(
    *, user_id: str, message: str, timezone: str
) -> AsyncIterator[tuple[str, dict]]:
    """Handle a message that opens with "/". Never retrieves, never answers."""
    resolved = slash.resolve(message)

    if resolved.kind == slash.KIND_HELP:
        yield EV_TOKEN, {"text": help_text()}
        return

    if resolved.kind == slash.KIND_UNKNOWN:
        log.info("chat.slash_unknown", user_id=user_id, name=resolved.raw_name)
        yield EV_TOKEN, {"text": f"I don't know `/{resolved.raw_name}`.\n\n{help_text()}"}
        return

    command = resolved.command
    if command is None:
        # `resolve` only returns KIND_COMMAND with a command attached, so this
        # is unreachable — it exists to narrow the type rather than to handle
        # a case, and returning silently beats raising on a user's turn.
        log.warning("chat.slash_resolution_without_command", user_id=user_id)
        return

    if command.fixed_action is not None:
        # Nothing to extract — `/catchup` *is* the action. Trailing text is
        # ignored rather than parsed, so the card always says what will run.
        # (`yield from` is a syntax error in an async generator, hence the loop.)
        for event in _propose([dict(command.fixed_action)], ""):
            yield event
        return

    if not resolved.args:
        yield EV_TOKEN, {"text": f"`/{command.name}` needs a bit more. Try:\n\n`{command.usage}`"}
        return

    yield EV_STAGE, {"label": "Working out what to change"}
    parsed = await run_in_threadpool(
        parse_command, None, resolved.args, timezone, command.action_types
    )
    proposed = parsed.get("actions") or []
    if not proposed:
        # A strict slash rule means saying so. Falling through to an answer
        # would silently swallow a change the user explicitly asked for.
        log.info("chat.slash_no_actions", user_id=user_id, name=command.name)
        yield (
            EV_TOKEN,
            {"text": f"I couldn't work out what to change from that. Try:\n\n`{command.usage}`"},
        )
        return

    log.info("chat.actions_proposed", user_id=user_id, name=command.name, count=len(proposed))
    for event in _propose(proposed, parsed.get("summary") or ""):
        yield event


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
    if slash.resolve(message).kind != slash.KIND_NONE:
        async for event in _command_events(user_id=user_id, message=message, timezone=timezone):
            yield event
        return

    yield EV_STAGE, {"label": "Reading your question"}

    intent = await _intent(user_id, message, history)

    if intent.kind == INTENT_SMALLTALK:
        async for event in stream_smalltalk(message, history):
            yield event
        return

    if not gmail_connected:
        # A user with no mailbox connected has a more immediate problem than
        # command syntax, so no nudge here even for INTENT_COMMAND.
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
    async for event in stream_answer(message, history, excerpts):
        yield event

    if intent.kind == INTENT_COMMAND and intent.command:
        log.info("chat.nudged", user_id=user_id, command=intent.command)
        yield EV_TOKEN, {"text": _nudge(intent.command, message)}
