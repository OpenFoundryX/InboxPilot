"""One chat turn, as a stream of events.

Deliberately free of database access: the API layer owns persistence and this
module owns the decision-making. That keeps the interesting logic — intent
routing, event ordering, graceful degradation — unit-testable with nothing but
fakes.

Two paths. If the message parses as a command, the engine *proposes* the
actions and stops; execution needs an explicit confirm. Otherwise it retrieves
context and streams a grounded answer.
"""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from core.config import settings
from core.logging import get_logger
from services.chat.describe import describe_actions
from services.chat.sources.base import Excerpt, Retriever
from services.commands.ask import ANSWER_RULES
from services.commands.parser import parse_command

log = get_logger(__name__)

EV_STAGE = "stage"
EV_TOKEN = "token"
EV_SOURCES = "sources"
EV_ACTIONS = "actions"

# How many prior turns are replayed to the answering model.
HISTORY_TURNS = 6

NOT_CONNECTED_MESSAGE = (
    "I can't read your mail yet — your Gmail account isn't connected. "
    "Connect it on the [setup page](/onboarding/connect) and ask me again."
)

_CHAT_ANSWER_SYS = ANSWER_RULES + (
    "\n\nThis is a live chat, not an email: do NOT sign off, and do not repeat the "
    "user's question back to them. The sources are also listed separately beneath "
    "your answer, so keep inline links to the ones you actually reference."
)


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


def _excerpts_as_corpus(excerpts: list[Excerpt]) -> str:
    """Render excerpts the same way the email flow renders its hits.

    Email excerpts already carry the fields `build_corpus` wants, so reuse it
    and keep one corpus format across both surfaces.
    """
    blocks = []
    for ex in excerpts:
        atts = f"{ex.attachment_count} file(s)" if ex.attachment_count else "none"
        blocks.append(
            f"From: {ex.sender}\nDate: {ex.date}\nSubject: {ex.title}\n"
            f"Attachments: {atts}\nLink: {ex.link}\n{ex.text}"
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

    turns = [
        {"role": h["role"], "content": h["content"]}
        for h in history[-HISTORY_TURNS:]
        if h.get("content")
    ]
    messages = [
        {"role": "system", "content": _CHAT_ANSWER_SYS},
        *turns,
        {"role": "user", "content": f"Question: {message}{context}"},
    ]

    stream = await _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0.3,
        messages=messages,
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


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

    from fastapi.concurrency import run_in_threadpool

    # `parse_command` reads a subject line too; chat has none.
    parsed = await run_in_threadpool(parse_command, None, message, timezone)
    actions = parsed.get("actions") or []
    if actions:
        log.info("chat.actions_proposed", user_id=user_id, count=len(actions))
        yield EV_ACTIONS, {
            "actions": describe_actions(actions),
            "raw": actions,
            "summary": parsed.get("summary") or "",
        }
        return

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
