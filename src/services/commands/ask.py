"""Answer a question the user emails themselves, using their inbox as context.

Powers "Ask anything". A single Gmail query rarely finds everything ("did Pradeep
send me the Mahindra users Excel?" needs sender + keyword + attachment + file-type
searches). So we ask the LLM to plan *several* complementary queries, run them all,
cast a wide net, dedupe the hits, then answer grounded in the actual messages —
including who sent what and which files were truly attached vs. only quoted in a body.
"""

import json
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from core import tokens
from integrations.google import gmail
from schemas.email import EmailSummary

log = get_logger(__name__)

# How many distinct searches to run, and how many messages to keep overall.
#
# These used to be 5/12/6, which starved the answer: twelve messages truncated to
# 800 characters each is ~2,000 tokens of evidence for a model that can hold
# fifty times that. Retrieval was the bottleneck, not the model — so cast the
# wide net the planner was designed for, and let ranking decide what survives.
_MAX_QUERIES = 5
_MAX_SOURCES = 40
_PER_QUERY = 12

# How much of each message body reaches the prompt. Enough for a real email
# rather than its opening paragraph.
_EXCERPT_CHARS = 4000

# Top-ranked hits that get their surrounding thread pulled in. A matched message
# is often a reply whose meaning lives in what came before it, and answering from
# the reply alone is a reliable way to produce something confident and wrong.
_THREAD_CONTEXT_TOP = 6
_THREAD_SIBLINGS = 3
_THREAD_SIBLING_CHARS = 1200
_THREAD_FETCH_CONCURRENCY = 4

# Words carrying no retrieval signal; scoring ignores them so that "what are my
# emails about" does not score every message for containing "are".
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "you",
        "your",
        "yours",
        "his",
        "her",
        "その",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "have",
        "has",
        "had",
        "did",
        "does",
        "do",
        "any",
        "all",
        "can",
        "will",
        "would",
        "about",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "how",
        "me",
        "my",
        "mine",
        "i",
        "is",
        "it",
        "its",
        "of",
        "on",
        "in",
        "to",
        "a",
        "an",
        "at",
        "by",
        "or",
        "as",
        "be",
        "been",
        "am",
        "send",
        "sent",
        "email",
        "emails",
        "mail",
        "mails",
        "inbox",
        "show",
        "find",
        "list",
        "get",
        "give",
        "tell",
        "please",
        "recent",
        "latest",
        "last",
        "new",
    }
)

# Gmail operators that are meaningless without a value. The planner emits these
# occasionally — a bare `label:` was observed in production — and Gmail does not
# reject them, it quietly returns loosely-matched junk that then displaces real
# results under the source cap.
_VALUELESS_OPERATOR = re.compile(
    r"\b(label|from|to|cc|bcc|subject|filename|has|is|in|newer_than|older_than|"
    r"larger|smaller|category)\s*:\s*(?=(?:\s|$))",
    re.I,
)
_EMPTY_QUOTED_OPERATOR = re.compile(
    r"\b(label|from|to|cc|bcc|subject|filename|category)\s*:\s*[\"']\s*[\"']",
    re.I,
)

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

Be specific, never generic. This is the difference between a useful answer and a
useless one:
- Quote the actual details — amounts, dates, names, order numbers, decisions. A
  reply that could have been written without reading the emails is a failure.
- "You have some updates from a few senders" is worthless. "Sprinto invited you to
  interview (11 Aug), and LinkedIn says your post got 96 impressions" is the job.
- Never hedge with "it seems", "possibly", or "you may want to check" when the
  excerpts state the fact plainly. Say what they say.
- Some excerpts will be irrelevant — the search casts a wide net deliberately.
  Ignore them silently rather than mentioning everything you were given.

Some emails include an "Earlier in this thread" section. Use it: the matched
message is often a reply whose meaning depends on what preceded it.

Attachment awareness: each email shows how many files are attached. Distinguish a
real attached file from content merely quoted or forwarded inside an email body — if
a file was expected but the email has no attachment, say so. If the excerpts don't
contain the answer, say what you found, state plainly what's missing, and suggest a
concrete next step. Never invent emails, senders, or links."""

# The email surface signs off; the web chat renders a live transcript and must not.
_ANSWER_SYS = ANSWER_RULES + ("\n\nEnd your reply with a sign-off line containing only: — InboxOS")


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
        out = sanitize_queries([q for q in queries if isinstance(q, str)])[:_MAX_QUERIES]
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


def sanitize_queries(queries: list[str]) -> list[str]:
    """Drop or repair Gmail queries the planner got wrong.

    A dangling operator (`label:` with no value) is not an error to Gmail — it
    matches loosely and returns plausible-looking junk, which then competes for
    slots against real results. Stripping the operator and keeping whatever
    terms remain salvages the query's intent; a query left with nothing but
    whitespace is dropped entirely.
    """
    out: list[str] = []
    for raw in queries:
        if not isinstance(raw, str):
            continue
        cleaned = _EMPTY_QUOTED_OPERATOR.sub(" ", raw)
        cleaned = _VALUELESS_OPERATOR.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # A query reduced to bare booleans carries no constraint at all.
        if not cleaned or cleaned.upper() in {"OR", "AND", "-"}:
            log.info("ask.query_dropped", query=raw)
            continue
        if cleaned != raw.strip():
            log.info("ask.query_repaired", before=raw, after=cleaned)
        if cleaned not in out:
            out.append(cleaned)
    return out


def _terms(question: str | None) -> list[str]:
    """Content words from the question, for relevance scoring."""
    words = re.findall(r"[a-z0-9']{3,}", (question or "").lower())
    return [w for w in dict.fromkeys(words) if w not in _STOPWORDS]


def _score(
    hit: EmailSummary,
    terms: list[str],
    match_count: int,
    best_rank: int,
    now: datetime,
) -> float:
    """Rank a hit against the question.

    The strongest signal is `match_count` — how many of the planner's
    *complementary* queries independently surfaced this message. The queries
    approach the question from different angles by design, so agreement between
    them means far more than a keyword appearing once in a body.

    Deliberately deterministic rather than an embedding or an LLM rerank: this
    runs on every question, and a scoring pass that costs an API call would
    undo the latency the wider net already spends.
    """
    subject = (hit.subject or "").lower()
    sender = (hit.sender or "").lower()
    body = (hit.body or hit.snippet or "").lower()

    score = 3.0 * match_count
    # Gmail's own ordering within the query that ranked it best.
    score += 2.0 / (1.0 + best_rank)

    if terms:
        score += 2.5 * sum(1 for t in terms if t in subject)
        score += 2.0 * sum(1 for t in terms if t in sender)
        # Capped: a long newsletter mentioning a term ten times is not ten times
        # more relevant, it is just long.
        score += 0.5 * min(sum(1 for t in terms if t in body), 6)

    if hit.date:
        days = max(0.0, (now - hit.date).total_seconds() / 86400.0)
        score += 2.0 * math.exp(-days / 30.0)

    return score


def search_all(
    user_id: str,
    queries: list[str],
    per_query: int = _PER_QUERY,
    *,
    question: str | None = None,
) -> list[EmailSummary]:
    """Run every planned query, merge, rank, and keep the best `_MAX_SOURCES`.

    Every query runs. This used to stop as soon as the source cap filled, which
    quietly defeated the whole design: the planner emits queries graduated from
    narrow to broad, broad ones last, so the moment the narrow queries returned
    anything the wide net was never cast. A question whose answer sat outside
    the first two queries simply could not be answered.

    Broad keyword queries would otherwise echo back the user's own self-email
    (the question itself is `from:me`), so we exclude the user's mail from any
    query that doesn't already target a specific sender.
    """
    by_id: dict[str, EmailSummary] = {}
    match_count: dict[str, int] = defaultdict(int)
    best_rank: dict[str, int] = {}

    for q in queries:
        query = q if "from:" in q.lower() else f"{q} -from:me"
        try:
            hits = gmail.fetch_by_query(user_id, query, per_query, verbose=True)
        except Exception:
            log.warning("ask.query_failed", user_id=user_id, query=query, exc_info=True)
            continue

        log.info("ask.query_hits", user_id=user_id, query=query, hits=len(hits))
        for position, h in enumerate(hits):
            if not h.id:
                continue
            by_id.setdefault(h.id, h)
            match_count[h.id] += 1
            best_rank[h.id] = min(best_rank.get(h.id, 10_000), position)

    terms = _terms(question)
    now = datetime.now(timezone.utc)
    ranked = sorted(
        by_id.values(),
        key=lambda h: _score(h, terms, match_count[h.id or ""], best_rank.get(h.id or "", 0), now),
        reverse=True,
    )
    kept = ranked[:_MAX_SOURCES]

    log.info(
        "ask.search_done",
        user_id=user_id,
        queries=len(queries),
        unique_hits=len(by_id),
        kept=len(kept),
        ranked=bool(terms),
    )
    return kept


# Gmail web permalink to a conversation. The hex thread id from the Gmail API
# resolves directly in the URL fragment.
#
# The `/mail/u/<...>/` slot takes an account *index*, not an address. Putting the
# email there used to work and looked like the obvious way to pin the link to the
# right account, but Google stopped resolving that form once a fragment follows —
# every link built that way now returns a hard 404 ("your account is temporarily
# unavailable"), whichever account you are signed in as.
#
# So the index stays `0` and the address moves to `authuser`, which is Google's
# actual cross-account disambiguation parameter. It is a hint, not a guarantee:
# the server cannot know the browser's sign-in order, and `authuser` is not
# honoured consistently across Google properties. The point of this shape is that
# it degrades to the default account's Gmail — a working page — instead of a 404.
def thread_link(thread_id: str | None, account_email: str | None) -> str:
    if not thread_id:
        return "(no link)"
    if not account_email:
        return f"https://mail.google.com/mail/u/0/#all/{thread_id}"
    # Encode the whole address: a bare "+" in a query value decodes to a space,
    # which silently drops the sub-address from a "user+tag@" account.
    who = quote(account_email, safe="")
    return f"https://mail.google.com/mail/u/0/?authuser={who}#all/{thread_id}"


def thread_context(user_id: str, hits: list[EmailSummary]) -> dict[str, str]:
    """Earlier messages in the threads of the top hits, keyed by message id.

    A search matches one message, but the answer often lives in the exchange
    around it — a reply saying "yes, approved" means nothing without the request
    it answers. Only the highest-ranked hits get this: a thread fetch is a
    separate round trip each, and the tail of the ranking rarely earns one.
    """
    targets = [h for h in hits[:_THREAD_CONTEXT_TOP] if h.thread_id and h.id]
    if not targets:
        return {}

    def _one(hit: EmailSummary) -> tuple[str, str]:
        try:
            thread = gmail.get_thread(user_id, str(hit.thread_id))
        except Exception:
            log.warning("ask.thread_fetch_failed", thread_id=hit.thread_id)
            return str(hit.id), ""

        messages = thread.get("messages") or []
        if len(messages) <= 1:
            return str(hit.id), ""

        lines: list[str] = []
        # Most recent first, skipping the message already quoted in full.
        for raw in reversed(messages):
            if raw.get("id") == hit.id:
                continue
            summary = gmail._summarize(raw)
            text = (summary.body or summary.snippet or "").strip()
            if not text:
                continue
            when = summary.date.strftime("%d %b %Y") if summary.date else "unknown date"
            lines.append(f"  [{when}] {summary.sender}: {text[:_THREAD_SIBLING_CHARS]}")
            if len(lines) >= _THREAD_SIBLINGS:
                break

        return str(hit.id), "\n".join(reversed(lines))

    workers = max(1, min(_THREAD_FETCH_CONCURRENCY, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_one, targets))

    return {mid: text for mid, text in results if text}


def build_corpus(
    hits: list[EmailSummary],
    account_email: str | None,
    context: dict[str, str] | None = None,
) -> str:
    context = context or {}
    blocks = []
    for h in hits:
        n = len(h.attachments)
        atts = f"{n} file(s)" if n else "none"
        block = (
            f"From: {h.sender}\nDate: {h.date}\nSubject: {h.subject}\n"
            f"Attachments: {atts}\nLink: {thread_link(h.thread_id, account_email)}\n"
            f"{(h.body or h.snippet or '')[:_EXCERPT_CHARS]}"
        )
        if earlier := context.get(h.id or ""):
            block += f"\n\nEarlier in this thread:\n{earlier}"
        blocks.append(block)
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
    question = f"{subject or ''} {(body or '')[:1500]}".strip()
    queries = plan_queries(subject, body)
    hits = search_all(user_id, queries, question=question) if queries else []
    context = thread_context(user_id, hits) if hits else {}

    user_msg = f"Question:\nSubject: {subject or ''}\n{(body or '')[:1500]}"
    if hits:
        corpus = build_corpus(hits, account_email, context)
        user_msg += f"\n\nRelevant emails from their inbox:\n{corpus}"
    else:
        corpus = ""
        user_msg += "\n\n(No relevant emails were found in their inbox.)"

    # Answering is the step that decides whether the reply is specific or
    # waffle, and it is the one place here worth spending a bigger model on.
    # Planning stays on the cheap one: emitting four Gmail queries as JSON is
    # not where quality is won.
    resp = _client().chat.completions.create(
        model=settings.SUMMARY_MODEL,
        temperature=0.3,
        messages=[
            {"role": "system", "content": _ANSWER_SYS},
            {"role": "user", "content": user_msg},
        ],
    )
    # Exact, from the provider. The tiktoken estimate below is only useful for
    # deciding what to send; this is what was actually billed.
    usage = getattr(resp, "usage", None)
    log.info(
        "ask.answered",
        user_id=user_id,
        queries=queries,
        sources=len(hits),
        threads_expanded=len(context),
        corpus_tokens=tokens.count(corpus, settings.SUMMARY_MODEL),
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        model=settings.SUMMARY_MODEL,
    )
    return (resp.choices[0].message.content or "").strip() or "I couldn't find an answer."
