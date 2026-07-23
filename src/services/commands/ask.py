"""Answer a question the user emails themselves, using their inbox as context.

Powers "Ask anything". A single Gmail query rarely finds everything ("did Pradeep
send me the Mahindra users Excel?" needs sender + keyword + attachment + file-type
searches). So we ask the LLM to plan *several* complementary queries, run them all,
cast a wide net, dedupe the hits, then answer grounded in the actual messages —
including who sent what and which files were truly attached vs. only quoted in a body.
"""

import json
import re

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from integrations.composio import gmail
from schemas.email import EmailSummary

log = get_logger(__name__)

# How many distinct searches to run, and how many messages to keep overall.
_MAX_QUERIES = 5
_MAX_SOURCES = 12
_PER_QUERY = 6

_PLAN_SYS = """You plan Gmail searches to answer a question about the user's mailbox.
A single query misses things, so return 3-5 COMPLEMENTARY queries that cast a wide
net from different angles — graduated from narrow to broad:

1. sender + bare keywords            e.g.  from:pradeep mahindra users
2. sender + keywords + attachment    e.g.  from:pradeep mahindra has:attachment
3. broad keywords across the mailbox e.g.  mahindra users list
4. keywords + file type              e.g.  mahindra filename:xlsx OR mahindra filename:xls

Rules:
- Prefer BARE keywords over subject:. `subject:` matches only the subject line and
  misses matches in the body — use it rarely.
- Do NOT stack many narrow operators in one query (e.g. subject: AND filename: AND
  has:attachment together) — that finds nothing. Keep each query lean.
- Always include at least one BROAD keyword-only query over the whole mailbox.
- Use the person's name or email as from: when the question names a sender.
- Ignore the email's own Subject line; base the searches on the QUESTION itself.
  Only use the Subject if the body has no question.
- Use operators: from: to: has:attachment filename:EXT newer_than:Nd OR "phrase".

If the question can't be answered from email (greeting, small-talk, a note with no
request), set needs_search=false.

Return ONLY JSON: {"needs_search": true|false, "queries": ["query1", "query2", ...]}

Example — question "did Pradeep send me the Mahindra users excel sheet?":
{"needs_search": true, "queries": [
  "from:pradeep mahindra users",
  "from:pradeep mahindra has:attachment",
  "mahindra users list",
  "mahindra filename:xlsx OR mahindra filename:xls"
]}"""

_ANSWER_SYS = """You are InboxOS, the user's email assistant. Answer the user's
question directly and concisely, grounded ONLY in the email excerpts provided.

Be specific and cite senders, subjects and dates where it helps. When you reference
an email, ALWAYS include its Link so the user can click straight to it in Gmail.
The message is PLAIN TEXT, so paste the Link as a bare URL exactly as given
(e.g. Open: https://mail.google.com/...). NEVER wrap it in markdown like
[text](url) — brackets show as literal characters. Do NOT list attachment file names.

Attachment awareness: each email shows how many files are attached. Distinguish a
real attached file from content merely quoted or forwarded inside an email body — if
a file was expected but the email has no attachment, say so. If the excerpts don't
contain the answer, say what you found, state plainly what's missing, and suggest a
concrete next step. Never invent emails, senders, or links.

End your reply with a sign-off line containing only: — InboxOS"""


# Replies are sent as plain text, where markdown link syntax renders as literal
# brackets. Collapse [label](url) -> "label (url)" so the URL stays clickable.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _plain_links(text: str) -> str:
    return _MD_LINK.sub(lambda m: f"{m.group(1)}: {m.group(2)}", text)


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _plan_queries(subject: str | None, body: str | None) -> list[str]:
    content = f"Subject: {subject or ''}\n\n{(body or '')[:1500]}"
    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _PLAN_SYS},
            {"role": "user", "content": content},
        ],
    )
    try:
        data = json.loads(resp.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        return []
    if not data.get("needs_search"):
        return []
    queries = data.get("queries") or []
    out: list[str] = []
    for q in queries:
        q = (q or "").strip() if isinstance(q, str) else ""
        if q and q not in out:
            out.append(q)
    return out[:_MAX_QUERIES]


def _search_all(user_id: str, queries: list[str]) -> list[EmailSummary]:
    """Run every planned query, merge results, dedupe by message id.

    Broad keyword queries would otherwise echo back the user's own self-email
    (the question itself is `from:me`), so we exclude the user's mail from any
    query that doesn't already target a specific sender.
    """
    by_id: dict[str, EmailSummary] = {}
    for q in queries:
        query = q if "from:" in q.lower() else f"{q} -from:me"
        try:
            hits = gmail.fetch_by_query(user_id, query, _PER_QUERY)
        except Exception:
            log.warning("ask.query_failed", user_id=user_id, query=query, exc_info=True)
            continue
        for h in hits:
            if h.id and h.id not in by_id:
                by_id[h.id] = h
            if len(by_id) >= _MAX_SOURCES:
                break
        if len(by_id) >= _MAX_SOURCES:
            break
    return list(by_id.values())


# Gmail web permalink to a conversation. `u/0` targets the primary signed-in
# account; the hex thread id from the Gmail API resolves directly in the fragment.
_THREAD_URL = "https://mail.google.com/mail/u/0/#all/{thread_id}"


def _thread_link(thread_id: str | None) -> str:
    return _THREAD_URL.format(thread_id=thread_id) if thread_id else "(no link)"


def _corpus(hits: list[EmailSummary]) -> str:
    blocks = []
    for h in hits:
        n = len(h.attachments)
        atts = f"{n} file(s)" if n else "none"
        blocks.append(
            f"From: {h.sender}\nDate: {h.date}\nSubject: {h.subject}\n"
            f"Attachments: {atts}\nLink: {_thread_link(h.thread_id)}\n"
            f"{(h.body or h.snippet or '')[:800]}"
        )
    return "\n\n---\n\n".join(blocks)


def answer_question(user_id: str, subject: str | None, body: str | None) -> str:
    """Return a grounded answer to the user's question (threaded reply text)."""
    queries = _plan_queries(subject, body)
    hits = _search_all(user_id, queries) if queries else []

    user_msg = f"Question:\nSubject: {subject or ''}\n{(body or '')[:1500]}"
    if hits:
        user_msg += f"\n\nRelevant emails from their inbox:\n{_corpus(hits)}"
    else:
        user_msg += "\n\n(No relevant emails were found in their inbox.)"

    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": _ANSWER_SYS},
            {"role": "user", "content": user_msg},
        ],
    )
    log.info(
        "ask.answered",
        user_id=user_id,
        queries=queries,
        sources=len(hits),
    )
    answer = (resp.choices[0].message.content or "").strip()
    return _plain_links(answer) or "I couldn't find an answer."
