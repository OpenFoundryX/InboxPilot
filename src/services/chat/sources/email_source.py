"""Gmail as a chat context source.

Reuses the query planner and multi-query search that already power the
email-based "Ask anything" flow, so retrieval quality improves in both places
at once. The Composio calls underneath are blocking, hence the threadpool.
"""

from fastapi.concurrency import run_in_threadpool

from core.logging import get_logger
from schemas.email import EmailSummary
from services.chat.sources.base import Excerpt, history_preamble
from services.commands import ask

log = get_logger(__name__)

# Smaller than the email flow's page size: a chat answer wants speed more than
# exhaustiveness, and the corpus is capped downstream anyway.
PER_QUERY = 5


class EmailRetriever:
    kind = "email"

    def __init__(self, account_email: str | None = None) -> None:
        # Pins "View email" links to the right account when several Google
        # accounts share a browser.
        self.account_email = account_email

    async def retrieve(
        self, user_id: str, question: str, history: list[dict]
    ) -> list[Excerpt]:
        # Question first: `ask.plan_queries` head-slices its input to 1500
        # chars, and the preamble alone can run to several hundred — putting
        # the live question last risked slicing it off entirely.
        body = f"Current question: {question}\n\n{history_preamble(history)}"
        queries = await run_in_threadpool(ask.plan_queries, None, body)
        if not queries:
            log.info("chat.no_queries_planned", user_id=user_id)
            return []

        hits = await run_in_threadpool(ask.search_all, user_id, queries, PER_QUERY)
        log.info("chat.retrieved", user_id=user_id, queries=queries, hits=len(hits))
        return [self._excerpt(h) for h in hits]

    def _excerpt(self, hit: EmailSummary) -> Excerpt:
        return Excerpt(
            kind=self.kind,
            title=hit.subject,
            sender=hit.sender,
            date=hit.date.isoformat() if hit.date else None,
            link=ask.thread_link(hit.thread_id, self.account_email),
            text=(hit.body or hit.snippet or "")[:800],
            ref_id=hit.id,
            thread_id=hit.thread_id,
            attachment_count=len(hit.attachments),
        )
