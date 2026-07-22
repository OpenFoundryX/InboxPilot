"""Answer a question the user emails themselves, using their inbox as context.

Powers "Ask anything": turn the question into a Gmail search, pull the most
relevant messages, and answer grounded in them. Falls back to a plain
conversational answer when nothing relevant is found.
"""

import json

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from integrations.composio import gmail

log = get_logger(__name__)

_QUERY_SYS = """Turn the user's question into a single Gmail search query that would
surface the emails needed to answer it. Use Gmail operators (from:, subject:,
newer_than:, has:attachment, OR, quotes). Return ONLY JSON: {"query": "...", "needs_search": true|false}.
Set needs_search=false for greetings/small-talk/notes that don't reference the user's mail."""


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _build_query(subject: str | None, body: str | None) -> str | None:
    content = f"Subject: {subject or ''}\n\n{(body or '')[:1500]}"
    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _QUERY_SYS},
            {"role": "user", "content": content},
        ],
    )
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return None
    if not data.get("needs_search"):
        return None
    return (data.get("query") or "").strip() or None


def answer_question(user_id: str, subject: str | None, body: str | None) -> str:
    """Return a grounded answer to the user's question (threaded reply text)."""
    query = _build_query(subject, body)

    context = ""
    used = 0
    if query:
        try:
            hits = gmail.fetch_by_query(user_id, f"{query} -from:me", 8)
        except Exception:
            hits = []
        used = len(hits)
        blocks = []
        for h in hits:
            blocks.append(
                f"From: {h.sender}\nDate: {h.date}\nSubject: {h.subject}\n{(h.body or h.snippet or '')[:800]}"
            )
        context = "\n\n---\n\n".join(blocks)

    sys = (
        "You are InboxOS, the user's email assistant. Answer their question concisely "
        "and helpfully, grounded in the email excerpts provided (cite senders/subjects "
        "where useful). If the excerpts don't contain the answer, say what you can and "
        "suggest a better search. Sign off as InboxOS."
    )
    user_msg = f"Question:\nSubject: {subject or ''}\n{(body or '')[:1500]}"
    if context:
        user_msg += f"\n\nRelevant emails from their inbox:\n{context}"
    else:
        user_msg += "\n\n(No inbox search was run or nothing relevant was found.)"

    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": user_msg},
        ],
    )
    log.info("ask.answered", user_id=user_id, query=query, sources=used)
    return (resp.choices[0].message.content or "").strip() or "I couldn't find an answer."
