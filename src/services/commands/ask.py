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

# Browse-style asks the planner sometimes marks needs_search=false when it
# doesn't know a keyword strategy. These still need the mailbox.
_MAILBOX_BROWSE = re.compile(
    r"\b(show|find|list|see|any|what(?:'s| is| are)?|which|summar|"
    r"catch\s*me\s*up|inbox|email|mail|unread|important|starred)\b",
    re.I,
)

# Safe default when the planner refuses a browse question about "important".
_IMPORTANT_FALLBACK = [
    'label:"to do" newer_than:30d',
    'label:"to follow up" newer_than:30d',
    "is:important newer_than:30d",
    'label:"fyi" newer_than:30d',
]

_PLAN_SYS = """You plan Gmail searches to answer a question about the user's mailbox.
A single query misses things, so return 3-5 COMPLEMENTARY queries that cast a wide
net from different angles — graduated from narrow to broad:

1. sender + bare keywords            e.g.  from:pradeep mahindra users
2. sender + keywords + attachment    e.g.  from:pradeep mahindra has:attachment
3. broad keywords across the mailbox e.g.  mahindra users list
4. keywords + file type              e.g.  mahindra filename:xlsx OR mahindra filename:xls

This mailbox is classified into InboxOS labels. Prefer those when the question is
about a category or "important" mail:
- "to do" — needs action/reply
- "to follow up" — waiting on someone / chase
- "fyi" — relevant, no action
- "notification" — receipts, alerts, system mail
- "marketing" / "noise" — promos and clutter (usually exclude these from "important")

Operators you may use: from: to: is:unread is:starred is:important has:attachment
filename:EXT newer_than:Nd older_than:Nd label:"name" OR "phrase".

Rules:
- Prefer BARE keywords over subject:. `subject:` matches only the subject line and
  misses matches in the body — use it rarely.
- Do NOT stack many narrow operators in one query (e.g. subject: AND filename: AND
  has:attachment together) — that finds nothing. Keep each query lean.
- For keyword/sender hunts, always include at least one BROAD keyword-only query.
- For browse/list questions ("show me…", "what's in…", "any…", "catch me up"),
  prefer label:/is: operators over bare keywords — do NOT invent fake keywords.
- Use the person's name or email as from: when the question names a sender.
- Ignore the email's own Subject line; base the searches on the QUESTION itself.
  Only use the Subject if the body has no question.
- needs_search=true whenever the user wants to READ, FIND, SHOW, LIST, COUNT, or
  SUMMARISE mail — including "show me my important emails", "what's in my inbox",
  "any invoices?", "catch me up". Only set needs_search=false for pure greetings
  or small-talk with no mailbox request.

Return ONLY JSON: {"needs_search": true|false, "queries": ["query1", "query2", ...]}

Example — question "did Pradeep send me the Mahindra users excel sheet?":
{"needs_search": true, "queries": [
  "from:pradeep mahindra users",
  "from:pradeep mahindra has:attachment",
  "mahindra users list",
  "mahindra filename:xlsx OR mahindra filename:xls"
]}

Example — question "Show me my important emails":
{"needs_search": true, "queries": [
  "label:\"to do\" newer_than:30d",
  "label:\"to follow up\" newer_than:30d",
  "is:important newer_than:30d",
  "label:\"fyi\" newer_than:30d"
]}"""

ANSWER_RULES = """You are InboxOS, the user's email assistant. Answer the user's
question directly and concisely, grounded ONLY in the email excerpts provided.

Format the reply in light Markdown for a clean email:
- Use **bold** for key facts (names, dates, the direct answer).
- Whenever you mention more than one email, you MUST put each on its own line as a
  bullet ("- "). Never run several emails together in one paragraph.
- When you reference an email, ALWAYS link to it as a Markdown link using its Link,
  with a short label — e.g. [View email](https://mail.google.com/...). Never paste a
  raw URL and never list attachment file names.
- Start an email's line with the link, then the sender and any date after it —
  e.g. "- [Invoice #42](https://mail.google.com/...) from Acme (due 3 Mar)".
- If several emails share a subject and sender, do not repeat identical lines. Add
  whatever tells them apart (the date it arrived, the amount, the account) so each
  line is distinguishable, and say how many there are.
- Keep it tight: a one-line direct answer first, then supporting detail.

Attachment awareness: each email shows how many files are attached. Distinguish a
real attached file from content merely quoted or forwarded inside an email body — if
a file was expected but the email has no attachment, say so. If the excerpts don't
contain the answer, say what you found, state plainly what's missing, and suggest a
concrete next step. Never invent emails, senders, or links."""

# The email surface signs off; the web chat renders a live transcript and must not.
_ANSWER_SYS = ANSWER_RULES + (
    "\n\nEnd your reply with a sign-off line containing only: — InboxOS"
)


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def plan_queries(subject: str | None, body: str | None) -> list[str]:
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
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ask.plan_bad_json", raw=raw[:500])
        return []
    needs_search = bool(data.get("needs_search"))
    queries = data.get("queries") or []
    out: list[str] = []
    if needs_search:
        for q in queries:
            q = (q or "").strip() if isinstance(q, str) else ""
            if q and q not in out:
                out.append(q)
        out = out[:_MAX_QUERIES]
    elif _MAILBOX_BROWSE.search(content):
        # Model said no search for an obvious mailbox browse — don't strand
        # the answer with an empty corpus. Prefer important-mail defaults when
        # the ask mentions importance; otherwise a recent-inbox net.
        if re.search(r"\bimportant\b", content, re.I):
            out = list(_IMPORTANT_FALLBACK)
        else:
            out = ["in:inbox newer_than:14d", "is:unread newer_than:14d"]
        log.warning(
            "ask.plan_forced_search",
            reason="browse_without_search",
            queries=out,
        )
    log.info(
        "ask.plan",
        needs_search=needs_search or bool(out),
        query_count=len(out),
        queries=out,
        raw_query_count=len(queries) if isinstance(queries, list) else 0,
    )
    return out


def search_all(
    user_id: str, queries: list[str], per_query: int = _PER_QUERY
) -> list[EmailSummary]:
    """Run every planned query, merge results, dedupe by message id.

    Broad keyword queries would otherwise echo back the user's own self-email
    (the question itself is `from:me`), so we exclude the user's mail from any
    query that doesn't already target a specific sender.
    """
    by_id: dict[str, EmailSummary] = {}
    for q in queries:
        query = q if "from:" in q.lower() else f"{q} -from:me"
        try:
            hits = gmail.fetch_by_query(user_id, query, per_query)
        except Exception:
            log.warning("ask.query_failed", user_id=user_id, query=query, exc_info=True)
            continue
        log.info(
            "ask.query_hits",
            user_id=user_id,
            query=query,
            hits=len(hits),
        )
        for h in hits:
            if h.id and h.id not in by_id:
                by_id[h.id] = h
            if len(by_id) >= _MAX_SOURCES:
                break
        if len(by_id) >= _MAX_SOURCES:
            break
    log.info("ask.search_done", user_id=user_id, unique_hits=len(by_id), queries=len(queries))
    return list(by_id.values())


# Gmail web permalink to a conversation. Putting the account's email address in the
# `/u/<...>/` slot pins the link to the right account even when several Google
# accounts are signed into the same browser (bare `u/0` would open the wrong one).
# The hex thread id from the Gmail API resolves directly in the URL fragment.
def thread_link(thread_id: str | None, account_email: str | None) -> str:
    if not thread_id:
        return "(no link)"
    slot = account_email or "0"
    return f"https://mail.google.com/mail/u/{slot}/#all/{thread_id}"


def build_corpus(hits: list[EmailSummary], account_email: str | None) -> str:
    blocks = []
    for h in hits:
        n = len(h.attachments)
        atts = f"{n} file(s)" if n else "none"
        blocks.append(
            f"From: {h.sender}\nDate: {h.date}\nSubject: {h.subject}\n"
            f"Attachments: {atts}\nLink: {thread_link(h.thread_id, account_email)}\n"
            f"{(h.body or h.snippet or '')[:800]}"
        )
    return "\n\n---\n\n".join(blocks)


def answer_question(
    user_id: str,
    subject: str | None,
    body: str | None,
    account_email: str | None = None,
) -> str:
    """Return a grounded answer to the user's question (threaded reply text).

    `account_email` pins the Gmail "View email" links to the right signed-in
    account; when omitted, links fall back to the primary account (`u/0`).
    """
    queries = plan_queries(subject, body)
    hits = search_all(user_id, queries) if queries else []

    user_msg = f"Question:\nSubject: {subject or ''}\n{(body or '')[:1500]}"
    if hits:
        user_msg += f"\n\nRelevant emails from their inbox:\n{build_corpus(hits, account_email)}"
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
    return (resp.choices[0].message.content or "").strip() or "I couldn't find an answer."
