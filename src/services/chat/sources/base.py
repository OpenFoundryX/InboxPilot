"""The retrieval seam.

Chat answers are grounded in `Excerpt`s. Today the only source is the user's
Gmail, but meeting notes and Notion are planned. Adding one means writing a new
`Retriever` in this package — the engine never changes, because it only ever
sees `Excerpt`s.
"""

from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

# How many prior turns of context a retriever folds into its query planning.
HISTORY_TURNS = 4


@dataclass
class Excerpt:
    """One retrieved piece of context, source-agnostic."""

    kind: str  # "email" | (later) "meetnote" | "notion"
    title: str | None
    sender: str | None
    date: str | None  # ISO 8601, or None
    link: str | None  # somewhere the user can open the original
    text: str
    ref_id: str | None = None  # the source system's id
    thread_id: str | None = None
    attachment_count: int = 0

    def as_dict(self) -> dict:
        """JSON-safe form, for the `sources` SSE event and the JSONB column."""
        return asdict(self)


@runtime_checkable
class Retriever(Protocol):
    kind: str

    async def retrieve(
        self, user_id: str, question: str, history: list[dict]
    ) -> list[Excerpt]: ...


def history_preamble(history: list[dict]) -> str:
    """Render recent turns so a follow-up like "the second one" can be resolved.

    `history` items are {"role": "user"|"assistant", "content": str}.
    """
    turns = [h for h in history if h.get("content")][-HISTORY_TURNS:]
    if not turns:
        return ""
    lines = [f"{h['role']}: {h['content'][:400]}" for h in turns]
    return "Earlier in this conversation:\n" + "\n".join(lines) + "\n\n"
