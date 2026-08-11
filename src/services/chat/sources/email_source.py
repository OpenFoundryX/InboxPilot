"""Gmail as a chat context source.

Reuses the query planner and multi-query search that already power the
email-based "Ask anything" flow, so retrieval quality improves in both places
at once. The Gmail calls underneath are blocking, hence the threadpool.
"""

from fastapi.concurrency import run_in_threadpool

from core.logging import get_logger
from schemas.email import EmailSummary
from services.chat.sources.base import Excerpt, history_preamble
from services.commands import ask

log = get_logger(__name__)

# Slightly smaller than the email flow's page size: a chat answer wants speed
# more than exhaustiveness. Every planned query still runs and the merged set is
# still ranked, so this trades breadth per query rather than dropping angles.
PER_QUERY = 10


class EmailRetriever:
    kind = "email"

    def __init__(self, account_email: str | None = None) -> None:
        # Pins "View email" links to the right account when several Google
        # accounts share a browser.
        self.account_email = account_email

    async def retrieve(self, user_id: str, question: str, history: list[dict]) -> list[Excerpt]:

        body = f"Current question: {question}\n\n{history_preamble(history)}"
        queries = await run_in_threadpool(ask.plan_queries, None, body)
        if not queries:
            # Planner decided the question needs no mailbox search (greeting,
            # small-talk leaked past intent, etc.) — chat still answers, just
            # with an empty corpus.
            log.info(
                "chat.no_queries_planned",
                user_id=user_id,
                question=(question or "")[:200],
            )
            return []

        hits = await run_in_threadpool(
            lambda: ask.search_all(user_id, queries, PER_QUERY, question=question)
        )
        # Ranked, so the threads worth expanding are the ones at the top.
        context = await run_in_threadpool(ask.thread_context, user_id, hits)
        log.info(
            "chat.retrieved",
            user_id=user_id,
            queries=queries,
            hits=len(hits),
            threads_expanded=len(context),
            question=(question or "")[:200],
        )
        return [self._excerpt(h, context) for h in hits]

    def _excerpt(self, hit: EmailSummary, context: dict[str, str]) -> Excerpt:
        text = (hit.body or hit.snippet or "")[: ask._EXCERPT_CHARS]
        if earlier := context.get(hit.id or ""):
            text += f"\n\nEarlier in this thread:\n{earlier}"
        return Excerpt(
            kind=self.kind,
            title=hit.subject,
            sender=hit.sender,
            date=hit.date.isoformat() if hit.date else None,
            link=ask.thread_link(hit.thread_id, self.account_email),
            text=text,
            ref_id=hit.id,
            thread_id=hit.thread_id,
            attachment_count=len(hit.attachments),
        )
